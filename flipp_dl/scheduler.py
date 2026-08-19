"""Background scheduler for flipp-dl.

Uses APScheduler (blocking scheduler) to run two recurring tasks:

poll_publications()
    Calls the Flipp API, upserts all publications/issues into the DB and
    queues a download job for every newly discovered issue in a watched
    publication.

run_download_queue()
    Picks up queued download jobs one at a time and executes them.

Both tasks share the same session factory and are designed to run in the
same process (simple self-hosted use case).  For higher throughput or
multi-process deployments, replace with Celery/RQ and promote the job
table to a proper broker.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from .api import FlippClient, FlippError
from .config import default_output_path, load_token
from .db.models import DbIssue
from .db.repository import DownloadRepository
from .db.session import get_session, make_session_factory
from .downloader import DEFAULT_WORKERS, IssueDownloader
from .models import Issue as DomainIssue
from .models import Publication as DomainPublication

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core tasks
# ---------------------------------------------------------------------------


def poll_publications(
    client: FlippClient,
    session_factory,
    output_root: Path,
    workers: int = DEFAULT_WORKERS,
) -> None:
    """Fetch publications from the Flipp API and queue new issues.

    Only issues belonging to *watched* publications are queued.
    """
    logger.info("Poll: fetching publications from Flipp API")
    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        job = repo.create_job("poll")

        try:
            repo.start_job(job.id)
            publications = client.fetch_publications()
            new_issues = repo.sync_publications(publications)
            logger.info(
                "Poll: %d publications synced, %d new issues found",
                len(publications),
                len(new_issues),
            )

            watched_pub_ids = {p.id for p in repo.list_publications(watched_only=True)}
            queued = 0
            for db_issue in new_issues:
                if db_issue.publication_id in watched_pub_ids:
                    repo.mark_issue_queued(db_issue.id)
                    repo.create_job("download", {"issue_id": db_issue.id})
                    queued += 1

            repo.finish_job(job.id)
            logger.info("Poll: queued %d new download jobs", queued)

            # Opportunistic retention sweep – cheap and keeps the Jobs
            # page from growing without bound on long-lived instances.
            purged = repo.purge_old_jobs()
            if purged:
                logger.info("Poll: purged %d old job rows", purged)
        except FlippError as exc:
            repo.finish_job(job.id, error=str(exc))
            logger.error("Poll failed: %s", exc)


def recover_stuck_jobs(session_factory) -> int:
    """Reset RUNNING download jobs left over from a crash/restart.

    Must be called once at startup, before the download queue starts
    draining, so previously-running jobs (and their issues) go back to
    QUEUED instead of sitting forever unclaimed. Returns the number of
    job rows reset.
    """
    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        reset = repo.reset_stuck_download_jobs()
    if reset:
        logger.info("Startup: reset %d stuck running download job(s) to queued", reset)
    return reset


def _claim_next_download_job(
    session_factory,
) -> tuple[int, DomainPublication, DomainIssue] | None:
    """Atomically mark the oldest queued download job as RUNNING.

    Returns ``(job_id, publication, issue)`` for the claimed job, or
    ``None`` when there is nothing to do. All ORM attribute access
    happens inside the single session so callers only ever see plain
    primitives / frozen dataclasses.
    """
    import json as _json

    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        job = repo.get_oldest_queued_job("download")
        if job is None:
            return None

        job_id: int = job.id
        try:
            payload = _json.loads(job.payload)
        except (TypeError, ValueError):
            repo.finish_job(job_id, error="Invalid JSON payload")
            return None
        issue_id = payload.get("issue_id")
        if issue_id is None:
            repo.finish_job(job_id, error="Missing issue_id in payload")
            return None

        db_issue: DbIssue | None = repo.get_issue(int(issue_id))
        if db_issue is None:
            repo.finish_job(job_id, error=f"Issue {issue_id} not found")
            return None

        db_pub = db_issue.publication
        domain_pub = DomainPublication(
            custom_code=db_pub.custom_code,
            name=db_pub.name,
        )
        domain_issue = DomainIssue(
            custom_code=db_issue.custom_code,
            issue_name=db_issue.issue_name,
            issue_date=db_issue.issue_date,
        )

        repo.start_job(job_id)
        return job_id, domain_pub, domain_issue


def run_download_queue(
    client: FlippClient,
    session_factory,
    output_root: Path,
    workers: int = DEFAULT_WORKERS,
    *,
    max_jobs: int | None = None,
) -> int:
    """Drain the queued download jobs and execute each in turn.

    The loop keeps pulling jobs until the queue is empty (or *max_jobs* has
    been reached), so one scheduler tick is enough to catch up after a
    manual bulk-queue or a long poll. Set ``max_jobs`` to cap throughput if
    you want to yield back to the scheduler more frequently. Returns the
    number of jobs that were actually executed.
    """
    processed = 0
    while max_jobs is None or processed < max_jobs:
        claimed = _claim_next_download_job(session_factory)
        if claimed is None:
            break
        job_id, domain_pub, domain_issue = claimed

        # Run the download in its own session so status commits are
        # immediately visible to the web UI between jobs.
        with get_session(session_factory) as session:
            repo = DownloadRepository(session)
            downloader = IssueDownloader(
                client, output_root, workers=workers, repository=repo
            )
            try:
                downloader.download_issue(domain_pub, domain_issue, skip_existing=True)
            except Exception as exc:  # noqa: BLE001
                with get_session(session_factory) as s2:
                    DownloadRepository(s2).finish_job(job_id, error=str(exc))
                logger.error("Download job %d failed: %s", job_id, exc)
                processed += 1
                continue

        with get_session(session_factory) as s2:
            DownloadRepository(s2).finish_job(job_id)
        processed += 1

    return processed


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------


def build_scheduler(
    *,
    db_path: Path,
    poll_interval_minutes: int = 360,
    download_interval_seconds: int = 30,
    workers: int = DEFAULT_WORKERS,
    output_root: Path | None = None,
) -> BlockingScheduler:
    """Return a configured :class:`BlockingScheduler`.

    Call ``.start()`` to block the current thread and run the scheduler.
    """
    token = load_token()
    if not token:
        raise RuntimeError(
            "No Flipp token found. Set FLIPP_TOKEN or create a `token` file."
        )

    client = FlippClient(token)
    session_factory = make_session_factory(db_path)
    out = output_root or default_output_path()
    recover_stuck_jobs(session_factory)

    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        poll_publications,
        trigger="interval",
        minutes=poll_interval_minutes,
        id="poll",
        kwargs=dict(
            client=client,
            session_factory=session_factory,
            output_root=out,
            workers=workers,
        ),
        next_run_time=None,  # manual first run below
    )

    scheduler.add_job(
        run_download_queue,
        trigger="interval",
        seconds=download_interval_seconds,
        id="download",
        kwargs=dict(
            client=client,
            session_factory=session_factory,
            output_root=out,
            workers=workers,
        ),
    )

    return scheduler


def run_scheduler(
    db_path: Path,
    *,
    poll_interval_minutes: int = 360,
    download_interval_seconds: int = 30,
    workers: int = DEFAULT_WORKERS,
    output_root: Path | None = None,
) -> None:
    """Start the scheduler and block until SIGINT/SIGTERM."""
    scheduler = build_scheduler(
        db_path=db_path,
        poll_interval_minutes=poll_interval_minutes,
        download_interval_seconds=download_interval_seconds,
        workers=workers,
        output_root=output_root,
    )

    def _shutdown(signum, _frame):
        logger.info("Received signal %d, shutting down scheduler", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info(
        "Scheduler starting – poll every %d min, download check every %d s",
        poll_interval_minutes,
        download_interval_seconds,
    )

    # Run an initial poll immediately before handing off to the loop.
    token = load_token()
    client = FlippClient(token)
    session_factory = make_session_factory(db_path)
    out = output_root or default_output_path()
    recover_stuck_jobs(session_factory)
    poll_publications(client, session_factory, out, workers)

    scheduler.start()
