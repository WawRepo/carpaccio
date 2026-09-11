"""Auth: HTTP Basic for scripts and agents, a signed session cookie for browsers.

Both paths satisfy the SAME middleware - Basic was not replaced, the cookie
was added beside it. An agent keeps sending Authorization: Basic and never
sees a redirect; a browser gets a real login form at /login and carries a
signed cookie afterwards.

WHY MIDDLEWARE AND NOT A PER-ROUTE DEPENDENCY. A dependency has to be added to
each route, so the failure mode is a route that silently has no auth — and the
route most likely to be forgotten is the next one somebody adds. Middleware is
deny-by-default: a new endpoint is protected the moment it exists, and opening
one up takes a deliberate edit to OPEN_PATHS below.

WHY /healthz STAYS OPEN, and it is not a convenience. Kubernetes liveness and
readiness probes send no credentials. Put auth in front of /healthz and the
probes get 401, the pod is declared unhealthy, and Kubernetes restarts it — for
ever. The app would be working perfectly and CrashLoopBackOff would say it was
not. /healthz reveals one boolean about S3 reachability and nothing else.

WHEN NO CREDENTIAL IS CONFIGURED, AUTH IS OFF — and /healthz says so out loud
(`"auth": "off"`). `docker compose up` on a laptop must work with no setup, but
"unauthenticated" must never be a state you can be in without being able to
see it. A silent default-open is how something ends up exposed and nobody
notices.
"""

import base64
import hmac
import logging
import os
import secrets
from urllib.parse import quote

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

# Open to unauthenticated callers. Keep this list as short as it is, and
# every entry deliberate:
#   /healthz - Kubernetes probes send no credentials (the whole comment above)
#   /login   - or nobody can ever log in
# /static/* is open too (prefix check in dispatch): the login page needs its
# stylesheet, and a login page that cannot load its own CSS is a page that
# looks broken to exactly the person who cannot log in yet. Static files are
# the committed CSS/JS of a public repo - there is nothing in them to guard.
OPEN_PATHS = frozenset({"/healthz", "/login"})
OPEN_PREFIXES = ("/static/",)

SESSION_COOKIE = "carpaccio_session"
# A week. Long enough that the owner is not retyping the password every
# morning on his own LAN; short enough that a cookie lifted from a lost
# laptop dies within days. Homelab numbers, chosen out loud.
SESSION_MAX_AGE = 7 * 24 * 3600

_REALM = 'Basic realm="carpaccio", charset="UTF-8"'


def session_secret_from_env() -> str:
    """The key that signs session cookies.

    Unset is allowed and must not stop the boot - but it must not be silent,
    and it must NEVER fall back to a constant: a secret in the source would
    let anyone who can read the repo forge a session for any instance that
    forgot to set the variable. A random per-boot secret keeps sessions
    unforgeable; the cost, said out loud in the log, is that every session
    dies with the process and everyone logs in again after a restart.
    """
    secret = os.environ.get("CARPACCIO_SESSION_SECRET", "").strip()
    if secret:
        return secret
    logging.getLogger("carpaccio").warning(
        "CARPACCIO_SESSION_SECRET is not set - using a random secret for this "
        "boot only. Sessions will NOT survive a restart; set the variable to "
        "make them durable."
    )
    return secrets.token_urlsafe(32)


def issue_session(secret: str, user: str) -> str:
    """A signed, timestamped token. itsdangerous, not hand-rolled crypto."""
    return TimestampSigner(secret).sign(user.encode()).decode()


def verify_session(secret: str, token: str, max_age: int = SESSION_MAX_AGE) -> str | None:
    """The username, or None for anything tampered, expired or malformed."""
    if not token:
        return None
    try:
        return TimestampSigner(secret).unsign(token, max_age=max_age).decode()
    except (BadSignature, SignatureExpired, UnicodeDecodeError):
        return None


def cookie_secure(request) -> bool:
    """Should the session cookie carry the Secure flag?

    THE TRAP, already paid for once elsewhere in this homelab: over plain
    HTTP a browser ACCEPTS a Secure cookie at login and then silently
    REFUSES TO SEND IT BACK - so login "succeeds", every next request has no
    cookie, bounces to /login, and neither the logs nor the browser show any
    error at all. A redirect loop with a clean console.

    In production this app sits behind Traefik over HTTPS and the flag must
    be ON (or the cookie leaks over any accidental http:// link). On a
    laptop, `docker compose up` is plain HTTP and the flag must be OFF or
    login loops as above. So: decide from how the request ACTUALLY arrived -
    X-Forwarded-Proto when a proxy set it (Traefik does), else the request
    scheme - with CARPACCIO_COOKIE_SECURE as an explicit override for setups
    this heuristic cannot see (a proxy that strips the header, TLS-
    terminating localhost tunnels).
    """
    override = os.environ.get("CARPACCIO_COOKIE_SECURE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


def wants_html(request) -> bool:
    """Is this a browser navigation, as opposed to an API call?

    An unauthenticated API client must get 401 JSON, NEVER a 302 to HTML:
    an agent that receives 200 with a login page will parse it as data and
    report success. Only a request that is not under /api/ AND explicitly
    prefers text/html (every browser navigation does; curl and httpx send
    */*) earns the redirect.
    """
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "")


def credentials_from_env() -> tuple[str, str] | None:
    """(user, bcrypt-hash), or None when auth is not configured."""
    user = os.environ.get("CARPACCIO_BASIC_USER", "").strip()
    pw_hash = os.environ.get("CARPACCIO_BASIC_PASSWORD_HASH", "").strip()
    if not user or not pw_hash:
        return None
    return user, pw_hash


def _unauthorised() -> JSONResponse:
    return JSONResponse(
        {"detail": "authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": _REALM},
    )


def check(user: str, password: str, expect_user: str, expect_hash: str) -> bool:
    """Both halves compared in constant time, and the order matters.

    `hmac.compare_digest` on the username rather than `==`: a plain comparison
    returns as soon as two bytes differ, so the time it takes leaks how much of
    the name was right. bcrypt is constant-time by construction.

    The password is checked even when the username is already wrong, so a bad
    username and a bad password take the same time. Returning early on the
    username would turn "is this a real account" into a stopwatch question.
    """
    user_ok = hmac.compare_digest(user.encode(), expect_user.encode())
    try:
        pw_ok = bcrypt.checkpw(password.encode(), expect_hash.encode())
    except ValueError:
        # A malformed hash in the environment must fail closed, not open.
        pw_ok = False
    return user_ok and pw_ok


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Basic AND session-cookie auth; either satisfies it. Deny by default."""

    def __init__(self, app, credentials: tuple[str, str] | None, session_secret: str = ""):
        super().__init__(app)
        self.credentials = credentials
        self.session_secret = session_secret

    async def dispatch(self, request, call_next):
        path = request.url.path
        if self.credentials is None or path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await call_next(request)

        # Path 1: HTTP Basic - agents, curl, the importer.
        header = request.headers.get("authorization", "")
        scheme, _, raw = header.partition(" ")
        if scheme.lower() == "basic" and raw:
            try:
                user, _, password = base64.b64decode(raw).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return _unauthorised()
            if check(user, password, *self.credentials):
                return await call_next(request)
            # Credentials were OFFERED and were wrong: always 401, even to a
            # browser - redirecting a failed Basic attempt to a form would
            # hide the failure from the script that made it.
            return _unauthorised()

        # Path 2: the session cookie a browser got from POST /login.
        token = request.cookies.get(SESSION_COOKIE, "")
        if verify_session(self.session_secret, token) is not None:
            return await call_next(request)

        # Neither. A browser navigation goes to the form and comes back to
        # where it was heading; everything else gets the honest 401.
        if wants_html(request):
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=302)
        return _unauthorised()
