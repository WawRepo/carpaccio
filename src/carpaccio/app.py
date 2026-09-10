"""The FastAPI app: the sheet UI plus a small JSON API.

NO AUTHENTICATION in v1, by explicit decision - this stays on the LAN.
See the Security section of the README.
"""

import json
from importlib import resources
from pathlib import Path

from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .model import load_schema, validation_errors
from .store import StaleVersion, Store

_PKG = Path(str(resources.files("carpaccio")))


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="cARPaccio", docs_url="/api/docs")
    app.state.store = store  # None means: build from env on first use
    templates = Jinja2Templates(directory=_PKG / "templates")
    app.mount("/static", StaticFiles(directory=_PKG / "static"), name="static")

    def get_store() -> Store:
        if app.state.store is None:
            app.state.store = Store.from_env()
        return app.state.store

    @app.get("/healthz")
    def healthz() -> dict:
        # Reports the S3 dependency but stays 200 while it is down: the pod is
        # alive and can say so, and restarting it would not fix MinIO.
        return {"status": "ok", "s3": "up" if get_store().healthy() else "down"}

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
    def get_network() -> JSONResponse:
        doc, version = get_store().load()
        return JSONResponse({"version": version, "document": doc}, headers={"ETag": version})

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

    return app


def build() -> FastAPI:
    """Entry point for `uvicorn carpaccio.app:build --factory`."""
    return create_app()
