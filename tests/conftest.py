import json
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from carpaccio.app import create_app
from carpaccio.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "unifi"


@pytest.fixture
def unifi_raw() -> dict:
    """The sanitised UniFi API responses, shaped exactly as the importer sees them."""
    raw = {
        name: json.loads((FIXTURES / f"{name}.json").read_text())["data"]
        for name in ("networkconf", "user", "sta", "device")
    }
    # The v2 firewall endpoint returns a bare list, [] on an unsegmented
    # network - which is the live state this tool exists to fix.
    raw["firewall_policies"] = []
    return raw


@pytest.fixture
def store():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="carpaccio")
        s3.put_bucket_versioning(Bucket="carpaccio", VersioningConfiguration={"Status": "Enabled"})
        yield Store(
            endpoint=None,
            access_key="test",
            secret_key="test",
            bucket="carpaccio",
            key="network.json",
        )


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(create_app(store))
