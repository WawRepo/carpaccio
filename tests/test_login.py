"""The login form beside HTTP Basic: both doors, one lock, and the traps.

The four precise behaviours, in test order: the redirect-vs-401 split (an
agent must NEVER receive a login page as data), the cookie lifecycle, the
Secure-flag decision (the silent redirect loop over plain HTTP), and the
per-boot random secret.
"""

import time

import bcrypt
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from carpaccio.app import create_app
from carpaccio.auth import SESSION_COOKIE, issue_session, verify_session

USER = "waw"
PASSWORD = "correct horse battery staple"
HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
SECRET = "test-session-secret"

HTML = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}  # a browser navigation


@pytest.fixture
def secured(store, monkeypatch) -> TestClient:
    monkeypatch.setenv("CARPACCIO_BASIC_USER", USER)
    monkeypatch.setenv("CARPACCIO_BASIC_PASSWORD_HASH", HASH)
    monkeypatch.setenv("CARPACCIO_SESSION_SECRET", SECRET)
    # follow_redirects off: the redirects ARE the behaviour under test
    return TestClient(create_app(store), follow_redirects=False)


def login(client, username=USER, password=PASSWORD, next="/"):
    return client.post("/login", data={"username": username, "password": password, "next": next})


# ---- 1. the redirect-vs-401 split, both ways --------------------------------


def test_browser_navigation_redirects_to_login(secured):
    r = secured.get("/", headers=HTML)
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=/"


def test_next_carries_the_original_destination(secured):
    r = secured.get("/?version=abc", headers=HTML)
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=/%3Fversion%3Dabc"


def test_api_calls_get_401_json_never_a_redirect(secured):
    # /api/* is an API whatever the Accept header says - a browser fetch()
    # sends text/html-ish Accept headers too and still must not get HTML.
    for headers in ({}, HTML):
        r = secured.get("/api/network", headers=headers)
        assert r.status_code == 401
        assert "basic" in r.headers.get("www-authenticate", "").lower()
        assert r.json()["detail"] == "authentication required"
    # and a non-API path WITHOUT html preference is an API client too
    assert secured.get("/").status_code == 401  # httpx default Accept: */*


def test_wrong_basic_credentials_get_401_even_with_html_accept(secured):
    """Credentials offered and wrong = 401 always: redirecting a failed Basic
    attempt to a form would hide the failure from the script that made it."""
    import base64

    raw = base64.b64encode(b"waw:wrong").decode()
    r = secured.get("/", headers={**HTML, "Authorization": f"Basic {raw}"})
    assert r.status_code == 401


# ---- open paths --------------------------------------------------------------


def test_login_page_and_static_are_reachable_unauthenticated(secured):
    page = secured.get("/login", headers=HTML)
    assert page.status_code == 200
    assert "cARPaccio" in page.text and 'name="password"' in page.text
    # the login page's own stylesheet - a login page that cannot load its CSS
    # is a broken-looking page shown to exactly the person who cannot log in
    css = secured.get("/static/style.css")
    assert css.status_code == 200
    assert secured.get("/healthz").status_code == 200


# ---- 2. the cookie lifecycle --------------------------------------------------


def test_good_login_sets_a_working_cookie(secured):
    r = login(secured)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert SESSION_COOKIE in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert f"max-age={7 * 24 * 3600}" in cookie.lower()
    # and the cookie then works, for the page AND the API
    assert secured.get("/", headers=HTML).status_code == 200
    assert secured.get("/api/network").status_code == 200


def test_page_shows_logout_only_for_a_session(secured):
    login(secured)
    assert "/logout" in secured.get("/", headers=HTML).text


def test_bad_login_sets_no_cookie_and_names_neither_field(secured):
    r = login(secured, password="wrong")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?error=1")
    assert "set-cookie" not in r.headers
    page = secured.get(r.headers["location"], headers=HTML).text
    assert "Wrong username or password." in page
    assert "Wrong password" not in page  # never says which half


def test_logout_clears_the_cookie(secured):
    login(secured)
    r = secured.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # the client honoured the deletion: back to the login redirect
    assert secured.get("/", headers=HTML).status_code == 302


def test_tampered_and_forged_cookies_are_refused(secured):
    login(secured)
    good = secured.cookies[SESSION_COOKIE]
    for bad in ("garbage", good[:-2] + "xx", issue_session("other-secret", USER)):
        secured.cookies.set(SESSION_COOKIE, bad)
        assert secured.get("/api/network").status_code == 401


def test_expired_session_is_refused():
    """A token stamped 8 days ago against the 7-day max age."""

    class OldSigner(TimestampSigner):
        def get_timestamp(self):
            return int(time.time()) - 8 * 24 * 3600

    old_token = OldSigner(SECRET).sign(USER.encode()).decode()
    assert verify_session(SECRET, old_token) is None
    fresh = issue_session(SECRET, USER)
    assert verify_session(SECRET, fresh) == USER


def test_basic_still_works_with_no_cookie(secured):
    """The other door did not close: agents never touch /login."""
    import base64

    raw = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    r = secured.get("/api/network", headers={"Authorization": f"Basic {raw}"})
    assert r.status_code == 200


# ---- open redirect guard ------------------------------------------------------


def test_next_cannot_leave_the_site(secured):
    for evil in ("https://evil.example/", "//evil.example", "javascript:alert(1)"):
        r = login(secured, next=evil)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


# ---- 3. the Secure flag -------------------------------------------------------


def test_secure_flag_follows_how_the_request_arrived(secured):
    # plain HTTP (docker compose on a laptop): Secure OFF, or the browser
    # accepts the cookie and silently never sends it back - the redirect
    # loop with clean logs that auth.cookie_secure documents
    r = login(secured)
    assert "secure" not in r.headers["set-cookie"].lower()
    # behind Traefik, which terminates TLS and says so:
    r2 = secured.post(
        "/login",
        data={"username": USER, "password": PASSWORD, "next": "/"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert "secure" in r2.headers["set-cookie"].lower()


def test_secure_flag_override(store, monkeypatch):
    monkeypatch.setenv("CARPACCIO_BASIC_USER", USER)
    monkeypatch.setenv("CARPACCIO_BASIC_PASSWORD_HASH", HASH)
    monkeypatch.setenv("CARPACCIO_SESSION_SECRET", SECRET)
    monkeypatch.setenv("CARPACCIO_COOKIE_SECURE", "true")
    client = TestClient(create_app(store), follow_redirects=False)
    r = client.post("/login", data={"username": USER, "password": PASSWORD, "next": "/"})
    assert "secure" in r.headers["set-cookie"].lower()


# ---- 4. the session secret ----------------------------------------------------


def test_missing_secret_warns_and_still_boots(store, monkeypatch, caplog):
    monkeypatch.setenv("CARPACCIO_BASIC_USER", USER)
    monkeypatch.setenv("CARPACCIO_BASIC_PASSWORD_HASH", HASH)
    monkeypatch.delenv("CARPACCIO_SESSION_SECRET", raising=False)
    with caplog.at_level("WARNING", logger="carpaccio"):
        client = TestClient(create_app(store), follow_redirects=False)
    assert any("will NOT survive a restart" in r.message for r in caplog.records)
    # sessions still work within this boot
    r = client.post("/login", data={"username": USER, "password": PASSWORD, "next": "/"})
    assert r.status_code == 303 and SESSION_COOKIE in r.headers["set-cookie"]
    assert client.get("/api/network").status_code == 200


def test_two_boots_without_a_secret_cannot_forge_each_other(store, monkeypatch):
    """The random fallback is per-process, not a constant: a cookie from one
    boot must be worthless on the next."""
    monkeypatch.setenv("CARPACCIO_BASIC_USER", USER)
    monkeypatch.setenv("CARPACCIO_BASIC_PASSWORD_HASH", HASH)
    monkeypatch.delenv("CARPACCIO_SESSION_SECRET", raising=False)
    boot1 = TestClient(create_app(store), follow_redirects=False)
    boot2 = TestClient(create_app(store), follow_redirects=False)
    boot1.post("/login", data={"username": USER, "password": PASSWORD, "next": "/"})
    token = boot1.cookies[SESSION_COOKIE]
    boot2.cookies.set(SESSION_COOKIE, token)
    assert boot2.get("/api/network").status_code == 401


def test_login_page_when_auth_is_off_redirects_home(store, monkeypatch):
    monkeypatch.delenv("CARPACCIO_BASIC_USER", raising=False)
    monkeypatch.delenv("CARPACCIO_BASIC_PASSWORD_HASH", raising=False)
    client = TestClient(create_app(store), follow_redirects=False)
    assert client.get("/login").status_code == 302


def test_login_page_is_revalidated(secured):
    """HOME-371: the login page carries the asset fingerprints, so it must never
    be served from a stale cache."""
    r = secured.get("/login", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "/static/style.css?v=" in r.text
