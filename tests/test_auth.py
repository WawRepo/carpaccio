"""HTTP Basic auth. The interesting cases are the two that fail OPEN.

1. /healthz must stay reachable without credentials. Kubernetes probes send
   none; behind auth they get 401, the pod is declared unhealthy and restarted
   for ever while the app works perfectly. That is the failure this suite
   exists to prevent, so it is the first test.
2. With no credential configured, auth is off -- deliberate, so `docker compose
   up` works on a laptop -- but /healthz must SAY so. A silent default-open is
   how something ends up exposed with nobody aware.
"""

import base64

import bcrypt
import pytest
from fastapi.testclient import TestClient

from carpaccio.app import create_app
from carpaccio.auth import check

USER = "waw"
PASSWORD = "correct horse battery staple"
HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


def _basic(user: str, password: str) -> dict:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def secured(store, monkeypatch) -> TestClient:
    monkeypatch.setenv("CARPACCIO_BASIC_USER", USER)
    monkeypatch.setenv("CARPACCIO_BASIC_PASSWORD_HASH", HASH)
    return TestClient(create_app(store))


def test_healthz_is_reachable_without_credentials(secured):
    """THE test. A 401 here means Kubernetes restarts a healthy pod for ever."""
    r = secured.get("/healthz")
    assert r.status_code == 200
    assert r.json()["auth"] == "on"


def test_everything_else_is_refused_without_credentials(secured):
    for path in ("/", "/api/network", "/api/schema", "/api/vlans", "/api/history"):
        r = secured.get(path)
        assert r.status_code == 401, f"{path} was reachable unauthenticated"
        assert "basic" in r.headers.get("www-authenticate", "").lower()


def test_writes_are_refused_too(secured):
    """A read-only check would miss the half that changes the network."""
    r = secured.patch("/api/devices/aa:bb:cc:dd:ee:ff", json={"notes": "x"})
    assert r.status_code == 401
    r = secured.put("/api/network", json={"version": "x", "document": {}})
    assert r.status_code == 401


def test_correct_credentials_are_accepted(secured):
    r = secured.get("/api/network", headers=_basic(USER, PASSWORD))
    assert r.status_code == 200
    assert "document" in r.json()


def test_wrong_password_and_wrong_user_are_both_refused(secured):
    assert secured.get("/api/network", headers=_basic(USER, "nope")).status_code == 401
    assert secured.get("/api/network", headers=_basic("root", PASSWORD)).status_code == 401


def test_malformed_authorization_headers_are_refused(secured):
    for header in (
        {"Authorization": "Basic !!!not-base64!!!"},
        {"Authorization": "Bearer sometoken"},
        {"Authorization": "Basic "},
        {"Authorization": ""},
    ):
        assert secured.get("/api/network", headers=header).status_code == 401


def test_unconfigured_means_open_and_says_so(store, monkeypatch):
    monkeypatch.delenv("CARPACCIO_BASIC_USER", raising=False)
    monkeypatch.delenv("CARPACCIO_BASIC_PASSWORD_HASH", raising=False)
    client = TestClient(create_app(store))
    assert client.get("/api/network").status_code == 200
    assert client.get("/healthz").json()["auth"] == "off"


def test_a_malformed_hash_fails_closed(store, monkeypatch):
    """A corrupted secret must lock everyone out, never let everyone in."""
    monkeypatch.setenv("CARPACCIO_BASIC_USER", USER)
    monkeypatch.setenv("CARPACCIO_BASIC_PASSWORD_HASH", "not-a-bcrypt-hash")
    client = TestClient(create_app(store))
    assert client.get("/api/network", headers=_basic(USER, PASSWORD)).status_code == 401


def test_check_rejects_a_wrong_username_even_with_the_right_password():
    assert check(USER, PASSWORD, USER, HASH) is True
    assert check("someone-else", PASSWORD, USER, HASH) is False


def test_icons_are_served_without_credentials(secured):
    """HOME-383. The browser fetches the tab icon for the LOGIN page, before it
    has a session, so the files must sit on a path the middleware leaves open.
    A 401 here would show as the generic globe - silently. Checked with auth ON:
    on an unauthenticated app this test would pass for the wrong reason."""
    for href in ("/static/favicon.ico", "/static/icon-192.png", "/static/apple-touch-icon.png"):
        r = secured.get(href)
        assert r.status_code == 200, (href, r.status_code)
        assert r.headers["content-type"].startswith("image/"), (href, r.headers["content-type"])
        assert len(r.content) > 500, href
