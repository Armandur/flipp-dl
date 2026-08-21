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
from ..editions import run_editions_queue
from ..scheduler import (
    poll_publications,
    recover_stuck_jobs,
    run_download_queue,
    run_komga_read_status_sync,
    run_komga_sync_queue,
)
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

# Release rows left half-finished by a crash or restart. This runs even
# without a token: the scheduler stays off, but the UI should never show
# a queued row that nothing will ever pick up.
_session_factory = make_session_factory(_db_path)
recover_stuck_jobs(_session_factory)

# The client is created once here and reused by both scheduled jobs (the
# same instance is baked into their kwargs), but its ``.token`` attribute
# is refreshed on every run from ``resolve_current_token`` - see
# scheduler.py. That's what lets a token saved via /settings take effect
# on the next tick without restarting this process, even when no token
# was available at all when the module was first imported.
_token = load_token()
_client = FlippClient(_token)

_scheduler = BackgroundScheduler(timezone="UTC")
_scheduler.add_job(
    poll_publications,
    trigger="interval",
    minutes=_poll_interval,
    id="poll",
    next_run_time=None,  # _initial_poll thread handles the first run
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
_scheduler.add_job(
    run_komga_sync_queue,
    trigger="interval",
    seconds=30,
    id="komga_sync",
    kwargs=dict(session_factory=_session_factory),
)
# This module is the Docker entrypoint, so every scheduled job has to be
# registered here too - build_scheduler() only covers the CLI process.
_scheduler.add_job(
    run_komga_read_status_sync,
    trigger="interval",
    hours=24,
    id="komga_read_status_sync",
    kwargs=dict(session_factory=_session_factory),
)
# The edition discovery/import runs the settings page queues. One at a
# time and coalesced - a run takes minutes, and a tick that fires while
# one is still working should be dropped, not stacked up behind it.
_scheduler.add_job(
    run_editions_queue,
    trigger="interval",
    seconds=60,
    id="editions",
    coalesce=True,
    max_instances=1,
    kwargs=dict(session_factory=_session_factory),
)
_scheduler.start()

if _token:
    logger.info("Scheduler started – poll every %d min", _poll_interval)
else:
    logger.warning(
        "No Flipp token configured at startup – scheduler is running but "
        "polls will fail until a token is saved via /settings."
    )


# Fire an immediate poll in a daemon thread so startup isn't blocked.
def _initial_poll():
    try:
        poll_publications(_client, _session_factory, _output_root, _workers)
    except Exception as exc:
        logger.error("Initial poll failed: %s", exc)


threading.Thread(target=_initial_poll, daemon=True, name="initial-poll").start()


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
