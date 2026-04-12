"""FastAPI application factory for flipp-dl."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..db.session import make_session_factory
from .auth import AuthMiddleware
from .html_sanitize import sanitize_html

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Sentinel for the development default secret key. Any deployment that
# ships this value is vulnerable to session forgery because anyone who
# can read the source can sign a valid session cookie.
_DEFAULT_SECRET_KEY = "dev-secret-change-me"


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

    app.state.session_factory = session_factory
    app.state.templates = templates
    app.state.output_root = output

    from . import routes  # noqa: F401

    routes.register(app)

    return app
