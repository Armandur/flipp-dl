"""Combined entry point: runs the web server and scheduler in one process.

The APScheduler runs in a background thread while Uvicorn serves HTTP in
the main thread.  This is the recommended Docker entrypoint for simple
self-hosted deployments.  For higher-load setups, run the web server and
scheduler as separate containers using the same shared /data volume.

Usage:
    python -m flipp_dl.web.main
    uvicorn flipp_dl.web.main:app          # web only
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from ..api import FlippClient
from ..config import load_token
from ..db.session import make_session_factory
from ..scheduler import poll_publications, run_download_queue
from .app import create_app

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# -------------------------------------------------------------------------
# Shared state (resolved once at import time so Uvicorn workers can reuse)
# -------------------------------------------------------------------------

_db_path = Path(os.environ.get("FLIPP_DB", "flipp.db"))
_output_root = Path(os.environ.get("FLIPP_OUTPUT", "Output"))
_poll_interval = int(os.environ.get("FLIPP_POLL_INTERVAL", "360"))
_workers = int(os.environ.get("FLIPP_WORKERS", "4"))

# FastAPI application (importable as `flipp_dl.web.main:app` for Uvicorn)
app = create_app(db_path=_db_path, output_root=_output_root)

# -------------------------------------------------------------------------
# Background scheduler (starts when the module is loaded by Uvicorn)
# -------------------------------------------------------------------------

_token = load_token()
if _token:
    _client = FlippClient(_token)
    _session_factory = make_session_factory(_db_path)

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        poll_publications,
        trigger="interval",
        minutes=_poll_interval,
        id="poll",
        kwargs=dict(
            client=_client,
            session_factory=_session_factory,
            output_root=_output_root,
            workers=_workers,
        ),
    )
    _scheduler.add_job(
        run_download_queue,
        trigger="interval",
        seconds=30,
        id="download",
        kwargs=dict(
            client=_client,
            session_factory=_session_factory,
            output_root=_output_root,
            workers=_workers,
        ),
    )
    _scheduler.start()
    logger.info("Scheduler started – poll every %d min", _poll_interval)

    # Fire an immediate poll in a daemon thread so startup isn't blocked.
    def _initial_poll():
        poll_publications(_client, _session_factory, _output_root, _workers)

    threading.Thread(target=_initial_poll, daemon=True, name="initial-poll").start()
else:
    logger.warning(
        "FLIPP_TOKEN not set – scheduler disabled. "
        "Set FLIPP_TOKEN and restart to enable automatic downloads."
    )


# -------------------------------------------------------------------------
# __main__ entry point (python -m flipp_dl.web.main)
# -------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "flipp_dl.web.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
