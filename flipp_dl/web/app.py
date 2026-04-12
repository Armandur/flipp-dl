"""FastAPI application factory for flipp-dl."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from ..db.session import make_session_factory

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(
    *,
    db_path: Path | None = None,
    output_root: Path | None = None,
) -> FastAPI:
    db = db_path or Path(os.environ.get("FLIPP_DB", "flipp.db"))
    output = output_root or Path(os.environ.get("FLIPP_OUTPUT", "Output"))

    session_factory = make_session_factory(db)

    app = FastAPI(title="flipp-dl", version="0.3.0", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Store shared state on app.state so routes can access it
    app.state.session_factory = session_factory
    app.state.templates = templates
    app.state.output_root = output

    from . import routes  # noqa: F401 – registers routes via import

    routes.register(app)

    return app
