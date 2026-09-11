"""One JSON document in S3 (MinIO), with an optimistic lock on the ETag.

Not a database and not git, on purpose: the bucket has object versioning
switched on, so S3 itself is the history/rollback mechanism.

Two writers (a human in the UI, an agent on the API) must never clobber each
other. Every read returns the object's ETag as an opaque version token; every
write must present the token it read. A write against a stale token is refused
with the current token and a description of what changed - never silently
merged, never last-writer-wins. Last-writer-wins on a firewall table is how a
VLAN quietly stops being separated.
"""

import json
import os
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .model import empty_document, summarise_changes

# Version token handed out when the object does not exist yet. A save that
# presents it succeeds only while the object is still absent.
MISSING = "__missing__"


class StaleVersion(Exception):
    def __init__(self, current_version: str, changes: list[str]):
        self.current_version = current_version
        self.changes = changes
        super().__init__("; ".join(changes))


class Store:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, key: str):
        self.bucket = bucket
        self.key = key
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            # MinIO wants path-style addressing; virtual-host style would try
            # to resolve <bucket>.<endpoint>.
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        # Serialises the compare-and-put below for writers sharing this
        # process. Cross-process the compare-and-put still has a tiny window;
        # accepted for a single-replica homelab tool, and the object
        # versioning on the bucket means even a lost race is recoverable.
        self._write_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "Store":
        return cls(
            endpoint=os.environ["CARPACCIO_S3_ENDPOINT"],
            access_key=os.environ["CARPACCIO_S3_ACCESS_KEY"],
            secret_key=os.environ["CARPACCIO_S3_SECRET_KEY"],
            bucket=os.environ.get("CARPACCIO_S3_BUCKET", "carpaccio"),
            key=os.environ.get("CARPACCIO_S3_KEY", "network.json"),
        )

    def load(self) -> tuple[dict, str]:
        """(document, version). A missing object is an empty document, not an error."""
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.key)
        except ClientError as err:
            if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return empty_document(), MISSING
            raise
        doc = json.loads(resp["Body"].read())
        return doc, resp["ETag"].strip('"')

    def save(self, doc: dict, expected_version: str) -> str:
        """Write the document; refuse with StaleVersion if someone wrote first.

        Returns the new version token. The caller validates the document
        BEFORE calling this - an invalid document never reaches the bucket.
        """
        with self._write_lock:
            current_doc, current_version = self.load()
            if current_version != expected_version:
                raise StaleVersion(current_version, summarise_changes(current_doc, doc))
            body = json.dumps(doc, indent=2, sort_keys=True).encode()
            resp = self._s3.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=body,
                ContentType="application/json",
            )
            return resp["ETag"].strip('"')

    def list_versions(self, limit: int = 30) -> list[dict]:
        """Stored versions of the document, newest first.

        The bucket's object versioning IS the history mechanism - the app
        deliberately implements none of its own. This only surfaces what S3
        already keeps: version id, timestamp, size. Delete markers are
        skipped; restoring always writes a NEW version through save(), so
        nothing here ever deletes or rewrites history.
        """
        resp = self._s3.list_object_versions(Bucket=self.bucket, Prefix=self.key)
        versions = [
            {
                "version_id": v["VersionId"],
                "last_modified": v["LastModified"].isoformat(),
                "size": v["Size"],
                "is_latest": bool(v.get("IsLatest")),
            }
            for v in resp.get("Versions", [])
            if v["Key"] == self.key  # Prefix matches "network.json.bak" too
        ]
        # S3 (and MinIO, and moto) return versions newest-first per key.
        # Deliberately NOT re-sorted here: several saves can land in the same
        # second, and re-sorting on the timestamp would scramble exactly the
        # ties the backend already has in the right order.
        return versions[:limit]

    def load_version(self, version_id: str) -> dict:
        """One historical document. Raises KeyError for an unknown version id.

        Deliberately returns no lock token: a historical read must never be
        mistaken for the current version, and restoring goes through the
        normal save() path with the CURRENT token.
        """
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=self.key, VersionId=version_id)
        except ClientError as err:
            code = err.response["Error"]["Code"]
            if code in ("NoSuchVersion", "NoSuchKey", "InvalidArgument", "404"):
                raise KeyError(version_id) from err
            raise
        return json.loads(resp["Body"].read())

    def healthy(self) -> bool:
        try:
            self._s3.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False
