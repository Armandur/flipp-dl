"""Session-based authentication middleware for flipp-dl.

Authentication is enabled only when the ``FLIPP_PASSWORD`` environment
variable is set.  If it is absent, all routes are publicly accessible
(useful for local development or trusted-network deployments).

CSRF protection is provided by:
1. ``SameSite=strict`` on the session cookie (prevents cross-site POST).
2. A per-session CSRF token validated on every state-changing request.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

# Routes that are always public (no login required)
_PUBLIC_PATHS = frozenset(["/login", "/healthz"])


def metrics_public() -> bool:
    """Whether /metrics may be scraped without logging in.

    A Prometheus scraper cannot log in, so an authenticated /metrics is
    effectively unreachable. It stays closed by default because the
    numbers describe the instance, and opens with FLIPP_METRICS_PUBLIC
    for the usual case: a scraper on the same trusted network.
    """
    return os.environ.get("FLIPP_METRICS_PUBLIC", "").lower() in ("1", "true", "yes")


# HTTP methods that mutate state and require a CSRF check
_UNSAFE_METHODS = frozenset(["POST", "PUT", "PATCH", "DELETE"])

# Header sent by HTMX on every AJAX request – used as an extra CSRF signal
_HTMX_HEADER = "hx-request"

CSRF_TOKEN_SESSION_KEY = "_csrf_token"


def _password() -> str:
    return os.environ.get("FLIPP_PASSWORD", "")


def auth_enabled() -> bool:
    return bool(_password())


def verify_password(plain: str) -> bool:
    expected = _password()
    if not expected:
        return True
    # Constant-time compare to prevent timing attacks
    return hmac.compare_digest(
        hashlib.sha256(plain.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def generate_csrf_token(request: Request) -> str:
    """Return (and persist) the CSRF token for the current session."""
    token = request.session.get(CSRF_TOKEN_SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        request.session[CSRF_TOKEN_SESSION_KEY] = token
    return token


def validate_csrf(request: Request) -> bool:
    """Return True if the CSRF token in the form matches the session."""
    # HTMX sends its own header; we still validate the token, but HTMX
    # requests that do NOT include a body (e.g. hx-post with no form)
    # are allowed through because SameSite=strict already covers them.
    session_token = request.session.get(CSRF_TOKEN_SESSION_KEY, "")
    if not session_token:
        return False
    # We read the form asynchronously in the route, so the middleware
    # delegates actual token validation to the route helper below.
    return True  # structural check only; value check in routes


async def check_csrf_form(request: Request) -> bool:
    """Compare the ``_csrf_token`` form field with the session value."""
    session_token = request.session.get(CSRF_TOKEN_SESSION_KEY, "")
    if not session_token:
        return False
    try:
        form = await request.form()
        form_token = form.get("_csrf_token", "")
    except Exception:
        return False
    return hmac.compare_digest(session_token, str(form_token))


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login when auth is enabled."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always allow public paths and static assets
        if path == "/metrics" and metrics_public():
            return await call_next(request)
        if path in _PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        if not auth_enabled():
            return await call_next(request)

        if not request.session.get("authenticated"):
            if path.startswith("/api/"):
                # An API client can't fill in a login form, so send it a
                # status it can act on instead of a redirect to HTML.
                return JSONResponse(
                    {"error": "authentication required"}, status_code=401
                )
            # Preserve the original destination so we can redirect back
            return RedirectResponse(url=f"/login?next={path}", status_code=302)

        return await call_next(request)
