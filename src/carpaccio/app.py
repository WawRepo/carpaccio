"""The FastAPI app: the sheet UI plus a small JSON API.

HTTP Basic auth on everything except /healthz - see auth.py for why it is
middleware rather than a per-route dependency, and why the health check has to
stay open. With no credential configured auth is OFF and /healthz says so.

The API contract is the whole document: GET /api/network hands out a version
token, PUT /api/network must present it, a stale write is refused with 409.
The per-resource routes below are CONVENIENCE ON TOP of that contract, not a
replacement: each one reads the document, applies one change, and saves with
the version it just read, so it conflicts exactly like the whole-document PUT.
There is deliberately NO server-side retry on conflict - retrying would mean
re-applying the change to a document another writer just reshaped, which is
last-writer-wins wearing a different hat. The caller re-reads and decides.
"""

import json
import uuid
from importlib import resources
from pathlib import Path

from fastapi import Body, FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import BasicAuthMiddleware, credentials_from_env
from .model import describe_diff, empty_document, load_schema, validation_errors
from .store import StaleVersion, Store

_PKG = Path(str(resources.files("carpaccio")))

SECTIONS = ("wans", "vlans", "devices", "infrastructure", "firewall_rules")


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="cARPaccio", docs_url="/api/docs")
    app.state.store = store  # None means: build from env on first use
    app.state.credentials = credentials_from_env()
    app.add_middleware(BasicAuthMiddleware, credentials=app.state.credentials)
    templates = Jinja2Templates(directory=_PKG / "templates")
    app.mount("/static", StaticFiles(directory=_PKG / "static"), name="static")

    def get_store() -> Store:
        if app.state.store is None:
            app.state.store = Store.from_env()
        return app.state.store

    # ------------------------------------------------------------------ core

    @app.get("/healthz")
    def healthz() -> dict:
        # Reports the S3 dependency but stays 200 while it is down: the pod is
        # alive and can say so, and restarting it would not fix MinIO.
        #
        # `auth` is here so that "this instance is wide open" is a thing you can
        # SEE rather than a thing you have to remember. Unauthenticated is a
        # legitimate state on a laptop; being in it without knowing is not.
        return {
            "status": "ok",
            "s3": "up" if get_store().healthy() else "down",
            "auth": "on" if app.state.credentials else "off",
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        doc, version = get_store().load()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                # Escaping "</" keeps a malicious string value from closing
                # the <script> block; Jinja must then NOT entity-escape it
                # (entities are not decoded inside <script>), hence |safe in
                # the template.
                "document_json": json.dumps(doc).replace("</", "<\\/"),
                "version": version,
            },
        )

    @app.get("/api/schema")
    def api_schema() -> dict:
        return load_schema()

    @app.get("/api/network")
    def get_network(version: str | None = None) -> JSONResponse:
        if version is not None:
            # A HISTORICAL read. The key is named version_id, NOT version, on
            # purpose: the body of PUT /api/network requires "version", so a
            # client that blindly round-trips a historical response cannot
            # accidentally use it as a lock token - it gets a 400, not a
            # silent overwrite.
            try:
                doc = get_store().load_version(version)
            except KeyError:
                return JSONResponse({"detail": f"no stored version {version!r}"}, status_code=404)
            return JSONResponse({"version_id": version, "historical": True, "document": doc})
        doc, current = get_store().load()
        return JSONResponse({"version": current, "document": doc}, headers={"ETag": current})

    @app.put("/api/network")
    def put_network(payload: dict = Body(...)) -> JSONResponse:
        doc = payload.get("document")
        version = payload.get("version")
        if not isinstance(doc, dict) or not isinstance(version, str):
            return JSONResponse(
                {"detail": 'body must be {"version": <token from GET>, "document": {...}}'},
                status_code=400,
            )
        errors = validation_errors(doc)
        if errors:
            # Refused BEFORE the store is touched: the stored object stays as it was.
            return JSONResponse(
                {"detail": "document failed schema validation", "errors": errors},
                status_code=422,
            )
        try:
            new_version = get_store().save(doc, expected_version=version)
        except StaleVersion as stale:
            return JSONResponse(
                {
                    "detail": "someone else saved since you loaded - reload and redo your edit",
                    "current_version": stale.current_version,
                    "changes": stale.changes,
                },
                status_code=409,
            )
        return JSONResponse({"version": new_version}, headers={"ETag": new_version})

    # --------------------------------------------------------------- history

    @app.get("/api/history")
    def history(limit: int = 30) -> JSONResponse:
        """Stored versions, newest first, each with a plain-English summary of
        what it changed versus its predecessor.

        The bucket's object versioning is the history; this only reads it.
        Summaries are computed server-side (one extra S3 GET per version -
        trivial against MinIO on the LAN) so the UI and any agent see the
        same words, and so the diff logic stays in tested Python rather than
        being reimplemented in the frontend.
        """
        store = get_store()
        # Fetch one PAST the window so the oldest visible entry still has a
        # predecessor to diff against.
        versions = store.list_versions(limit=limit + 1)
        docs: list[dict | None] = []
        for v in versions:
            try:
                docs.append(store.load_version(v["version_id"]))
            except Exception:  # a torn read mid-lifecycle beats a 500
                docs.append(None)
        out = []
        for i, v in enumerate(versions[:limit]):
            entry = dict(v)
            newer = docs[i]
            older = docs[i + 1] if i + 1 < len(docs) else None
            if newer is None:
                entry["changes"] = ["(unreadable version)"]
            elif older is not None:
                entry["changes"] = describe_diff(older, newer) or ["no change in content"]
            elif len(versions) <= limit:
                # Genuinely the first version ever stored.
                entry["changes"] = describe_diff(empty_document(), newer) or ["created"]
            else:
                entry["changes"] = ["(older versions beyond this window)"]
            out.append(entry)
        return JSONResponse({"versions": out})

    # ------------------------------------------------- per-resource REST API

    def _collection(section: str):
        def read() -> JSONResponse:
            doc, version = get_store().load()
            return JSONResponse(
                {"version": version, section: doc[section]}, headers={"ETag": version}
            )

        return read

    for _section in SECTIONS:
        app.get(f"/api/{_section}", name=f"list_{_section}")(_collection(_section))

    def _write(mutate, if_match: str | None):
        """Read-modify-write one change under the same lock as the full PUT.

        `mutate(doc)` edits the loaded document in place and returns either
        the success payload (dict) or a JSONResponse to short-circuit with
        (404, 409-by-content, ...). An If-Match header pins the write to a
        version the CALLER read earlier; without it the write is pinned to
        the version read HERE, which still refuses any save that lands in
        between - no lost updates either way.
        """
        doc, version = get_store().load()
        if if_match is not None and if_match != version:
            return JSONResponse(
                {
                    "detail": "If-Match does not match the stored version",
                    "current_version": version,
                },
                status_code=409,
            )
        result = mutate(doc)
        if isinstance(result, JSONResponse):
            return result
        errors = validation_errors(doc)
        if errors:
            return JSONResponse(
                {"detail": "change would make the document invalid", "errors": errors},
                status_code=422,
            )
        try:
            new_version = get_store().save(doc, expected_version=version)
        except StaleVersion as stale:
            return JSONResponse(
                {
                    "detail": "someone else saved while this request ran - re-read and retry",
                    "current_version": stale.current_version,
                    "changes": stale.changes,
                },
                status_code=409,
            )
        return JSONResponse({"version": new_version, **result}, headers={"ETag": new_version})

    def _not_found(what: str) -> JSONResponse:
        return JSONResponse({"detail": what}, status_code=404)

    # -- devices, keyed by MAC ------------------------------------------------

    @app.post("/api/devices")
    def create_device(
        payload: dict = Body(...), if_match: str | None = Header(default=None)
    ) -> JSONResponse:
        def mutate(doc):
            mac = str(payload.get("mac", "")).strip().lower()
            payload["mac"] = mac
            if any(d["mac"] == mac for d in doc["devices"]):
                return JSONResponse(
                    {"detail": f"device {mac} already exists - PATCH it instead"},
                    status_code=409,
                )
            payload.setdefault("source", "manual")
            doc["devices"].append(payload)
            return {"device": payload}

        return _write(mutate, if_match)

    @app.patch("/api/devices/{mac}")
    def patch_device(
        mac: str, payload: dict = Body(...), if_match: str | None = Header(default=None)
    ) -> JSONResponse:
        def mutate(doc):
            mac_l = mac.lower()
            dev = next((d for d in doc["devices"] if d["mac"] == mac_l), None)
            if dev is None:
                return _not_found(f"no device {mac_l}")
            if "mac" in payload:
                new_mac = str(payload["mac"]).strip().lower()
                if new_mac != mac_l and any(d["mac"] == new_mac for d in doc["devices"]):
                    return JSONResponse(
                        {"detail": f"device {new_mac} already exists"}, status_code=409
                    )
                payload["mac"] = new_mac
            dev.update(payload)
            return {"device": dev}

        return _write(mutate, if_match)

    @app.delete("/api/devices/{mac}")
    def delete_device(mac: str, if_match: str | None = Header(default=None)) -> JSONResponse:
        def mutate(doc):
            mac_l = mac.lower()
            before = len(doc["devices"])
            doc["devices"] = [d for d in doc["devices"] if d["mac"] != mac_l]
            if len(doc["devices"]) == before:
                return _not_found(f"no device {mac_l}")
            return {"deleted": mac_l}

        return _write(mutate, if_match)

    # -- vlans, keyed by name ---------------------------------------------------

    @app.post("/api/vlans")
    def create_vlan(
        payload: dict = Body(...), if_match: str | None = Header(default=None)
    ) -> JSONResponse:
        def mutate(doc):
            name = payload.get("name", "")
            if any(v["name"] == name for v in doc["vlans"]):
                return JSONResponse({"detail": f"vlan {name!r} already exists"}, status_code=409)
            # Only the importer may mint 'unifi' rows: that marker means "the
            # controller will re-assert this row by name on every import", and
            # making it up over the API would hand the row's name to a merge
            # that has never heard of it.
            payload["source"] = "manual"
            doc["vlans"].append(payload)
            return {"vlan": payload}

        return _write(mutate, if_match)

    @app.patch("/api/vlans/{name}")
    def patch_vlan(
        name: str, payload: dict = Body(...), if_match: str | None = Header(default=None)
    ) -> JSONResponse:
        def mutate(doc):
            vlan = next((v for v in doc["vlans"] if v["name"] == name), None)
            if vlan is None:
                return _not_found(f"no vlan {name!r}")
            new_name = payload.get("name", name)
            if new_name != name:
                if vlan.get("source") != "manual":
                    return JSONResponse(
                        {
                            "detail": (
                                f"vlan {name!r} came from UniFi and its name is the merge "
                                "key: renaming it here would duplicate it on the next "
                                "import. Rename it on the controller instead."
                            )
                        },
                        status_code=409,
                    )
                if any(v["name"] == new_name for v in doc["vlans"]):
                    return JSONResponse(
                        {"detail": f"vlan {new_name!r} already exists"}, status_code=409
                    )
                # Devices reference their VLAN by name; a rename that left
                # them pointing at the old name would orphan them off every tab.
                for d in doc["devices"]:
                    if d.get("vlan") == name:
                        d["vlan"] = new_name
            vlan.update(payload)
            # source is not editable over PATCH either - same reason as POST.
            vlan["source"] = "manual" if vlan.get("source") == "manual" else "unifi"
            return {"vlan": vlan}

        return _write(mutate, if_match)

    @app.delete("/api/vlans/{name}")
    def delete_vlan(name: str, if_match: str | None = Header(default=None)) -> JSONResponse:
        def mutate(doc):
            if not any(v["name"] == name for v in doc["vlans"]):
                return _not_found(f"no vlan {name!r}")
            still_on_it = sum(1 for d in doc["devices"] if d.get("vlan") == name)
            if still_on_it:
                # Refusing beats silently orphaning rows off every tab. Move
                # or delete the devices first - deliberately manual, because
                # bulk-deleting devices is not something a DELETE on a VLAN
                # should ever imply.
                return JSONResponse(
                    {
                        "detail": (
                            f"vlan {name!r} still has {still_on_it} device(s) on it - "
                            "move or delete them first"
                        ),
                        "devices_on_vlan": still_on_it,
                    },
                    status_code=409,
                )
            doc["vlans"] = [v for v in doc["vlans"] if v["name"] != name]
            return {"deleted": name}

        return _write(mutate, if_match)

    # -- firewall rules, keyed by id -------------------------------------------

    @app.post("/api/firewall_rules")
    def create_rule(
        payload: dict = Body(...), if_match: str | None = Header(default=None)
    ) -> JSONResponse:
        def mutate(doc):
            rule_id = payload.get("id") or f"manual-{uuid.uuid4().hex[:8]}"
            if any(r.get("id") == rule_id for r in doc["firewall_rules"]):
                return JSONResponse({"detail": f"rule {rule_id!r} already exists"}, status_code=409)
            payload["id"] = rule_id
            doc["firewall_rules"].append(payload)
            return {"firewall_rule": payload}

        return _write(mutate, if_match)

    @app.patch("/api/firewall_rules/{rule_id}")
    def patch_rule(
        rule_id: str, payload: dict = Body(...), if_match: str | None = Header(default=None)
    ) -> JSONResponse:
        def mutate(doc):
            rule = next((r for r in doc["firewall_rules"] if r.get("id") == rule_id), None)
            if rule is None:
                return _not_found(f"no firewall rule {rule_id!r}")
            payload.pop("id", None)  # the id is the key; rename by delete+create
            rule.update(payload)
            return {"firewall_rule": rule}

        return _write(mutate, if_match)

    @app.delete("/api/firewall_rules/{rule_id}")
    def delete_rule(rule_id: str, if_match: str | None = Header(default=None)) -> JSONResponse:
        def mutate(doc):
            before = len(doc["firewall_rules"])
            doc["firewall_rules"] = [r for r in doc["firewall_rules"] if r.get("id") != rule_id]
            if len(doc["firewall_rules"]) == before:
                return _not_found(f"no firewall rule {rule_id!r}")
            return {"deleted": rule_id}

        return _write(mutate, if_match)

    return app


def build() -> FastAPI:
    """Entry point for `uvicorn carpaccio.app:build --factory`."""
    return create_app()
