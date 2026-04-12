"""FastAPI application factory for flipp-dl."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..db.session import make_session_factory
from .auth import AuthMiddleware
from .html_sanitize import sanitize_html

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Secret key for signing session cookies.
# Override with a long random string in production via FLIPP_SECRET_KEY.
_SECRET_KEY = os.environ.get("FLIPP_SECRET_KEY", "dev-secret-change-me")


def create_app(
    *,
    db_path: Path | None = None,
    output_root: Path | None = None,
) -> FastAPI:
    db = db_path or Path(os.environ.get("FLIPP_DB", "flipp.db"))
    output = output_root or Path(os.environ.get("FLIPP_OUTPUT", "Output"))

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

    app.state.session_factory = session_factory
    app.state.templates = templates
    app.state.output_root = output

    from . import routes  # noqa: F401

    routes.register(app)

    return app
