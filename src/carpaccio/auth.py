"""HTTP Basic auth, applied as middleware to everything except the health check.

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
import os

import bcrypt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Open to unauthenticated callers. Keep this list as short as it is.
OPEN_PATHS = frozenset({"/healthz"})

_REALM = 'Basic realm="carpaccio", charset="UTF-8"'


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
    def __init__(self, app, credentials: tuple[str, str] | None):
        super().__init__(app)
        self.credentials = credentials

    async def dispatch(self, request, call_next):
        if self.credentials is None or request.url.path in OPEN_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, raw = header.partition(" ")
        if scheme.lower() != "basic" or not raw:
            return _unauthorised()
        try:
            user, _, password = base64.b64decode(raw).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return _unauthorised()

        if not check(user, password, *self.credentials):
            return _unauthorised()
        return await call_next(request)
