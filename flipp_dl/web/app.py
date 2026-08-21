"""FastAPI application factory for flipp-dl."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jinja2
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..db.session import make_session_factory
from .auth import AuthMiddleware
from .html_sanitize import sanitize_html
from .i18n import get_language, translate

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Sentinel for the development default secret key. Any deployment that
# ships this value is vulnerable to session forgery because anyone who
# can read the source can sign a valid session cookie.
_DEFAULT_SECRET_KEY = "dev-secret-change-me"


def _display_timezone() -> ZoneInfo:
    """Timezone the UI renders timestamps in.

    ``FLIPP_TZ`` wins, then the container's own ``TZ``, then Swedish
    time - this is a self-hosted tool for a Swedish publisher's app.
    An unknown zone name falls back rather than breaking every page.
    """
    for name in (os.environ.get("FLIPP_TZ"), os.environ.get("TZ")):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown timezone %r - falling back", name)
    return ZoneInfo("Europe/Stockholm")


def _localtime(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a stored (naive, UTC) timestamp in the display timezone.

    Timestamps are written as naive UTC by ``repository._now()``, so a
    value without tzinfo is assumed to be UTC rather than local.
    """
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_display_timezone()).strftime(fmt)


@jinja2.pass_context
def _translate_in_context(context: jinja2.runtime.Context, message: str) -> str:
    """Jinja global ``_()`` - looks up the active request's language and
    translates *message*, falling back to the English msgid untouched.

    Reads the language from the render context (populated by FastAPI's
    ``Jinja2Templates`` with the ``request`` object) instead of a
    process-global, so concurrent requests in different languages never
    interfere with each other.
    """
    request = context.get("request")
    lang = get_language(request) if request is not None else "en"
    return translate(lang, message)


def _human_size(num_bytes: int | None) -> str:
    """Format a byte count as a short human-readable string (KB / MB / GB)."""
    if num_bytes is None or num_bytes < 0:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# Secret key for signing session cookies.
# Override with a long random string in production via FLIPP_SECRET_KEY.
_SECRET_KEY = os.environ.get("FLIPP_SECRET_KEY", _DEFAULT_SECRET_KEY)


def create_app(
    *,
    db_path: Path | None = None,
    output_root: Path | None = None,
) -> FastAPI:
    db = db_path or Path(os.environ.get("FLIPP_DB", "flipp.db"))
    output = output_root or Path(os.environ.get("FLIPP_OUTPUT", "Output"))

    if _SECRET_KEY == _DEFAULT_SECRET_KEY:
        logger.warning(
            "FLIPP_SECRET_KEY is not set – falling back to the public "
            "development default. Session cookies can be forged by "
            "anyone with access to the source. Set FLIPP_SECRET_KEY to "
            "a long random string (e.g. `python -c 'import secrets; "
            "print(secrets.token_hex(32))'`) before exposing the UI."
        )

    session_factory = make_session_factory(db)

    app = FastAPI(title="flipp-dl", version="0.3.0", docs_url=None, redoc_url=None)

    # Starlette executes middleware in reverse addition order (last added = outermost).
    # AuthMiddleware must be added FIRST so SessionMiddleware runs before it,
    # ensuring request.session is populated when AuthMiddleware inspects it.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_SECRET_KEY,
        same_site="strict",
        https_only=False,  # set True behind TLS in production
        session_cookie="flipp_session",
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["sanitize_html"] = sanitize_html
    templates.env.filters["human_size"] = _human_size
    templates.env.filters["localtime"] = _localtime
    templates.env.globals["_"] = _translate_in_context
    templates.env.globals["current_language"] = get_language

    app.state.session_factory = session_factory
    app.state.templates = templates
    app.state.output_root = output
    app.state.db_path = Path(db)

    from . import api_routes, codes_routes, path_browser, routes  # noqa: F401

    routes.register(app)
    api_routes.register(app)
    codes_routes.register(app)
    path_browser.register(app)

    return app
