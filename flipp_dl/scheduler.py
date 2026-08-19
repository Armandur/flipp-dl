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

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from . import storage
from .api import FlippClient, FlippError, build_session
from .config import default_output_path, load_token
from .db.models import DbIssue
from .db.repository import (
    DownloadRepository,
    default_cover_cache_root,
    fetch_and_cache_cover,
)
from .db.session import get_session, make_session_factory
from .downloader import DEFAULT_WORKERS, IssueDownloader, purge_old_previews
from .komga import (
    DEFAULT_WAIT_SECONDS,
    ISSUE_METADATA_FIELDS,
    PUBLICATION_METADATA_FIELDS,
    KomgaClient,
    KomgaError,
    cover_push_enabled,
    filter_pushed_fields,
    html_to_plain_text,
    parse_issue_number,
)
from .models import Issue as DomainIssue
from .models import Publication as DomainPublication
from .notify import NotificationChannel, NtfyChannel, WebhookChannel, send_all

# Cover images are hosted at fixed resolution suffixes. Requesting the
# "300m" variant once and caching it is enough for both the publications
# list thumbnail (rendered at 44x60 via CSS) and the detail page (110x150) -
# no need to fetch two sizes per publication.
_COVER_SIZE_VARIANT = "__b300m."

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core tasks
# ---------------------------------------------------------------------------


def resolve_current_token(session_factory) -> str:
    """Return the token to use right now.

    A token saved via the /settings UI (``DbSetting`` key ``flipp_token``)
    always wins; otherwise fall back to ``FLIPP_TOKEN``/the ``token`` file.
    The client object is created once at process startup, so every caller
    that talks to the Flipp API re-resolves the token here instead of
    trusting whatever ``client.token`` happened to be set to at creation
    time - that's what lets a token saved in the UI take effect on the
    next poll/download tick without a restart.
    """
    with get_session(session_factory) as session:
        saved = DownloadRepository(session).get_setting("flipp_token", "").strip()
    return saved or load_token()


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_or_db(env_key: str, db_value: str) -> str:
    """Environment variable wins over a saved ``DbSetting``, if set at all."""
    env_value = os.environ.get(env_key)
    return env_value if env_value is not None else db_value


def resolve_komga_settings_from_repo(repo: DownloadRepository) -> dict:
    """Resolve the Komga settings from an already-open repository session.

    Env vars (``KOMGA_URL``, ``KOMGA_USERNAME``, ``KOMGA_PASSWORD``,
    ``KOMGA_API_KEY``, ``KOMGA_LIBRARY_ID``, ``KOMGA_ENABLED``) override the
    ``DbSetting`` rows saved via /settings, matching the acceptance
    criteria for TASK-1326 - the opposite precedence from
    :func:`resolve_current_token`, which is deliberate: Komga credentials
    are meant to be overridable per-deployment via the environment even
    when a value is already saved in the UI.
    """
    return {
        "url": _env_or_db("KOMGA_URL", repo.get_setting("komga_url", "")).strip(),
        "username": _env_or_db(
            "KOMGA_USERNAME", repo.get_setting("komga_username", "")
        ).strip(),
        "password": _env_or_db(
            "KOMGA_PASSWORD", repo.get_setting("komga_password", "")
        ),
        "api_key": _env_or_db("KOMGA_API_KEY", repo.get_setting("komga_api_key", "")),
        "library_id": _env_or_db(
            "KOMGA_LIBRARY_ID", repo.get_setting("komga_library_id", "")
        ).strip(),
        "enabled": _parse_bool(
            _env_or_db("KOMGA_ENABLED", repo.get_setting("komga_enabled", ""))
        ),
    }


def resolve_komga_settings(session_factory) -> dict:
    """Resolve the current Komga settings in their own short-lived session."""
    with get_session(session_factory) as session:
        return resolve_komga_settings_from_repo(DownloadRepository(session))


def resolve_notify_settings_from_repo(repo: DownloadRepository) -> dict:
    """Resolve the notification settings (TASK-1293).

    Two independent channels, each with its own enabled flag so either
    can be on, off, or both at once: ntfy (``NTFY_*`` / ``notify_ntfy_*``)
    and a generic webhook (``NOTIFY_WEBHOOK_*`` / ``notify_webhook_*``).
    Same env-overrides-DbSetting precedence as Komga - see
    :func:`resolve_komga_settings_from_repo`.
    """
    return {
        "ntfy_enabled": _parse_bool(
            _env_or_db("NTFY_ENABLED", repo.get_setting("notify_ntfy_enabled", ""))
        ),
        "ntfy_url": (
            _env_or_db("NTFY_URL", repo.get_setting("notify_ntfy_url", "")).strip()
            or "https://ntfy.sh"
        ),
        "ntfy_topic": _env_or_db(
            "NTFY_TOPIC", repo.get_setting("notify_ntfy_topic", "")
        ).strip(),
        "ntfy_token": _env_or_db(
            "NTFY_TOKEN", repo.get_setting("notify_ntfy_token", "")
        ),
        "webhook_enabled": _parse_bool(
            _env_or_db(
                "NOTIFY_WEBHOOK_ENABLED", repo.get_setting("notify_webhook_enabled", "")
            )
        ),
        "webhook_url": _env_or_db(
            "NOTIFY_WEBHOOK_URL", repo.get_setting("notify_webhook_url", "")
        ).strip(),
    }


def resolve_notify_settings(session_factory) -> dict:
    """Resolve the current notification settings in their own session."""
    with get_session(session_factory) as session:
        return resolve_notify_settings_from_repo(DownloadRepository(session))


def build_notify_channels(settings: dict) -> list[NotificationChannel]:
    """Instantiate the enabled notification channels from *settings*.

    Each channel is skipped (not sent broken) when enabled but missing
    its required field - an ntfy topic or a webhook URL - the same
    "incomplete config is not an error" rule TASK-1326 established for
    Komga.
    """
    channels: list[NotificationChannel] = []
    if settings["ntfy_enabled"] and settings["ntfy_topic"]:
        channels.append(
            NtfyChannel(
                settings["ntfy_url"], settings["ntfy_topic"], settings["ntfy_token"]
            )
        )
    if settings["webhook_enabled"] and settings["webhook_url"]:
        channels.append(WebhookChannel(settings["webhook_url"]))
    return channels


# How many issue titles a bundled notification lists by name before
# collapsing the rest into "... and N more" - keeps a normal few-issues
# tick readable while a large catch-up run still produces one short
# message instead of one per issue (or an unbounded wall of text).
_MAX_LISTED_ISSUES = 20


def _format_issue_list(items: list[str]) -> str:
    if len(items) <= _MAX_LISTED_ISSUES:
        return "\n".join(items)
    shown = items[:_MAX_LISTED_ISSUES]
    return "\n".join(shown) + f"\n... och {len(items) - _MAX_LISTED_ISSUES} till"


def _send_download_notifications(
    channels: list[NotificationChannel], successes: list[str], failures: list[str]
) -> None:
    """Send at most one summary notification per outcome for this drain.

    ``run_download_queue`` calls this once, after its loop over every job
    claimed in this call - not once per issue. A single scheduler tick
    normally drains one or two jobs, but a bulk backfill (TASK-1291 talks
    about ~17660 issues) can drain thousands in one call; one notification
    per issue there would flood the channel, so all of this call's
    successes are bundled into one message and all of its failures into
    another.
    """
    if successes:
        count = len(successes)
        title = (
            "1 ny utgåva nedladdad" if count == 1 else f"{count} nya utgåvor nedladdade"
        )
        send_all(channels, f"Flipp-DL: {title}", _format_issue_list(successes))
    if failures:
        count = len(failures)
        title = (
            "1 nedladdning misslyckades"
            if count == 1
            else f"{count} nedladdningar misslyckades"
        )
        send_all(channels, f"Flipp-DL: {title}", _format_issue_list(failures))


def _cache_covers(repo: DownloadRepository, new_issues: list[DbIssue]) -> int:
    """Fetch and store local copies of new/changed covers (TASK-1345).

    Runs once per poll tick, never during page rendering. Publication
    covers only refetch when ``cover_url`` actually changed since the
    last cache (:meth:`DownloadRepository.publications_needing_cover_refresh`),
    and issue covers are fetched exactly once, at discovery time - so the
    cache grows with what a poll actually finds, not with the full
    17660-issue back catalogue already sitting in the DB. Returns the
    number of covers newly cached, for logging.
    """
    cache_root = default_cover_cache_root()
    http = build_session()
    cached = 0

    for pub in repo.publications_needing_cover_refresh():
        url = pub.cover_url.replace("__b600m.", _COVER_SIZE_VARIANT)
        filename = fetch_and_cache_cover(
            url, cache_root, f"pub-{pub.custom_code}", http_session=http
        )
        if filename:
            repo.set_publication_cover_cache(pub.id, filename, pub.cover_url)
            cached += 1

    for issue in new_issues:
        url = (
            "https://edition.pagesuite-professional.co.uk/get_image.aspx"
            f"?w=100&eid={issue.custom_code}"
        )
        filename = fetch_and_cache_cover(
            url, cache_root, f"issue-{issue.custom_code}", http_session=http
        )
        if filename:
            repo.set_issue_cover_cache(issue.id, filename)
            cached += 1

    return cached


def poll_publications(
    client: FlippClient,
    session_factory,
    output_root: Path,
    workers: int = DEFAULT_WORKERS,
) -> None:
    """Fetch publications from the Flipp API and queue new issues.

    Only issues belonging to *watched* publications are queued.
    """
    client.token = resolve_current_token(session_factory)
    if not client.token:
        # The scheduler runs even without a token so one saved via
        # /settings takes effect without a restart. Polling anyway would
        # just fill the jobs log with failures every six hours.
        logger.info("Poll: no token configured yet - skipping")
        return
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

            cached_covers = _cache_covers(repo, new_issues)
            if cached_covers:
                logger.info("Poll: cached %d cover image(s)", cached_covers)

            watched_pub_ids = {p.id for p in repo.list_publications(watched_only=True)}
            # Publications with their own poll interval (TASK-1291) only
            # have new issues queued/backfilled once their interval has
            # elapsed; publications without an override are always due,
            # matching today's behaviour. The metadata sync above already
            # ran for every publication regardless - only the queuing
            # below is gated.
            due_pub_ids = repo.publications_due_for_poll(watched_pub_ids)

            queued = 0
            for db_issue in new_issues:
                if db_issue.publication_id in due_pub_ids:
                    repo.mark_issue_queued(db_issue.id)
                    repo.create_job("download", {"issue_id": db_issue.id})
                    queued += 1

            # Catch up on anything a due, watched publication never got:
            # an issue that existed before watching was turned on, one
            # that was lost to a restart, or one skipped on an earlier
            # tick because this publication wasn't due yet. Failed issues
            # stay out of this so a permanently broken issue isn't
            # retried every poll.
            backfilled = 0
            for pub_id in due_pub_ids:
                backfilled += repo.queue_missing_issues(pub_id, include_failed=False)
                repo.mark_publication_poll_done(pub_id)

            repo.finish_job(job.id)
            logger.info(
                "Poll: queued %d new download jobs, %d catching up",
                queued,
                backfilled,
            )

            # Opportunistic retention sweep – cheap and keeps the Jobs
            # page from growing without bound on long-lived instances.
            purged = repo.purge_old_jobs()
            if purged:
                logger.info("Poll: purged %d old job rows", purged)

            # Same idea for stray preview PDFs (TASK-1344) - they live
            # outside output_root and outside the DB, so this poll tick
            # is their only cleanup path.
            purged_previews = purge_old_previews()
            if purged_previews:
                logger.info("Poll: purged %d old preview file(s)", purged_previews)
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
        orphaned = repo.reset_orphaned_issues()
        duplicates = repo.release_duplicate_file_claims()
    if reset:
        logger.info("Startup: reset %d stuck running download job(s) to queued", reset)
    if orphaned:
        logger.info(
            "Startup: reset %d issue(s) stuck without a job to not-downloaded",
            orphaned,
        )
    if duplicates:
        logger.warning(
            "Startup: %d issue(s) claimed another issue's file and were "
            "marked not-downloaded - they will be fetched on the next poll",
            duplicates,
        )
    return reset


def _claim_next_download_job(
    session_factory,
) -> tuple[int, DomainPublication, DomainIssue, int] | None:
    """Atomically mark the oldest queued download job as RUNNING.

    Returns ``(job_id, publication, issue, issue_id)`` for the claimed
    job, or ``None`` when there is nothing to do. All ORM attribute
    access happens inside the single session so callers only ever see
    plain primitives / frozen dataclasses. ``issue_id`` is handed back
    alongside the domain dataclasses (which don't carry a DB id) so the
    caller can thread it into the ``komga_sync`` job payload (TASK-1327).
    """
    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        job = repo.get_oldest_queued_job("download")
        if job is None:
            return None

        job_id: int = job.id
        try:
            payload = json.loads(job.payload)
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
        return job_id, domain_pub, domain_issue, db_issue.id


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
    client.token = resolve_current_token(session_factory)
    if not client.token:
        return 0
    notify_channels = build_notify_channels(resolve_notify_settings(session_factory))
    notify_successes: list[str] = []
    notify_failures: list[str] = []
    processed = 0
    while max_jobs is None or processed < max_jobs:
        claimed = _claim_next_download_job(session_factory)
        if claimed is None:
            break
        job_id, domain_pub, domain_issue, issue_id = claimed
        issue_label = f"{domain_pub.name} - {domain_issue.issue_name}"

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
                notify_failures.append(issue_label)
                processed += 1
                continue

        notify_successes.append(issue_label)

        with get_session(session_factory) as s2:
            repo2 = DownloadRepository(s2)
            komga_settings = resolve_komga_settings_from_repo(repo2)
            # Queue a scan only when Komga is fully configured - an
            # incomplete configuration (enabled but no library picked yet)
            # must not produce a failed job in the log (TASK-1326).
            if (
                komga_settings["enabled"]
                and komga_settings["url"]
                and komga_settings["library_id"]
            ):
                # issue_id lets the komga_sync handler push this specific
                # issue's metadata once the scan it triggers has picked
                # the book up (TASK-1327) - the scan itself stays
                # library-wide, matching TASK-1326.
                repo2.create_job(
                    "komga_sync",
                    {
                        "library_id": komga_settings["library_id"],
                        "issue_id": issue_id,
                    },
                )
            repo2.finish_job(job_id)
        processed += 1

    if notify_channels and (notify_successes or notify_failures):
        _send_download_notifications(notify_channels, notify_successes, notify_failures)

    return processed


def _komga_wait_seconds() -> int:
    """``KOMGA_WAIT_SECONDS`` - how long to poll for a book after a scan."""
    raw = os.environ.get("KOMGA_WAIT_SECONDS", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_WAIT_SECONDS
    except ValueError:
        return DEFAULT_WAIT_SECONDS


def _wait_for_book(
    client: KomgaClient,
    series_id: int | str,
    stems: set[str],
    wait_seconds: int,
    *,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict | None:
    """Poll Komga's book list for *stems* until found or *wait_seconds* elapse.

    Komga's library scan (triggered right before this) runs
    asynchronously, so the book this sync is about may not be indexed
    yet. Polls at most once per second, always trying at least once even
    when ``wait_seconds`` is 0.
    """
    deadline = clock() + wait_seconds
    while True:
        book = client.find_book_by_stems(series_id, stems)
        if book is not None:
            return book
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(1, remaining))


def _push_publication_and_issue_metadata(
    session_factory,
    client: KomgaClient,
    library_id: str,
    issue_id: int,
    wait_seconds: int,
) -> str | None:
    """Map, then push, series + book metadata and the cover for one issue.

    Returns ``None`` on success, or an error string for the job log. A
    publication with no Komga series mapped yet is not an error - it
    just means the folder-name lookup hasn't matched (yet), which the
    detail page renders as "Komga: okänd - söker nästa gång" and the
    next successful sync tries again.
    """
    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        db_issue: DbIssue | None = repo.get_issue(issue_id)
        if db_issue is None:
            return f"Issue {issue_id} not found"
        db_pub = db_issue.publication

        series_id = db_pub.komga_series_id
        if series_id is None:
            folder_name = storage.safe_name(db_pub.name)
            series = client.find_series_by_name(library_id, folder_name)
            if series is None:
                logger.info(
                    "Komga sync: no series matched for publication %r yet - "
                    "will retry next sync",
                    db_pub.name,
                )
                return None
            series_id = series["id"]
            repo.set_komga_series_id(db_pub.custom_code, series_id)

        pub_fields = filter_pushed_fields(
            {
                "title": db_pub.name,
                "titleSort": db_pub.name,
                "summary": html_to_plain_text(db_pub.description),
                "publisher": "Egmont",
                "language": "sv",
                "genres": [c.category_name for c in db_pub.categories] or None,
                "tags": [c.category_name for c in db_pub.categories] or None,
            },
            PUBLICATION_METADATA_FIELDS,
        )

        cover_path = None
        if cover_push_enabled() and db_pub.cover_cache_path:
            cache_file = default_cover_cache_root() / db_pub.cover_cache_path
            cover_path = cache_file if cache_file.is_file() else None
            cover_filename = db_pub.cover_cache_path

        domain_pub = DomainPublication(custom_code=db_pub.custom_code, name=db_pub.name)
        domain_issue = DomainIssue(
            custom_code=db_issue.custom_code,
            issue_name=db_issue.issue_name,
            issue_date=db_issue.issue_date,
        )
        stems = {
            Path(storage.issue_filename(domain_pub, domain_issue, disambiguate=d)).stem
            for d in (False, True)
        }

    try:
        client.patch_series_metadata(series_id, **pub_fields)
        if cover_path is not None:
            client.upload_series_thumbnail(
                series_id, cover_path.read_bytes(), cover_filename
            )
    except (KomgaError, OSError) as exc:
        return f"Failed to push series metadata/cover for series {series_id}: {exc}"

    book = _wait_for_book(client, series_id, stems, wait_seconds)
    if book is None:
        return (
            f"Book for issue {domain_issue.issue_name!r} not found in Komga "
            f"series {series_id} after waiting {wait_seconds}s - run the "
            "sync again manually once Komga has finished scanning."
        )

    number, number_sort = parse_issue_number(domain_issue.issue_name)
    issue_fields = filter_pushed_fields(
        {
            "title": domain_issue.issue_name,
            "number": number,
            "numberSort": number_sort,
            "releaseDate": domain_issue.issue_date,
        },
        ISSUE_METADATA_FIELDS,
    )
    try:
        client.patch_book_metadata(book["id"], **issue_fields)
    except KomgaError as exc:
        return f"Failed to push book metadata for book {book['id']}: {exc}"

    return None


def run_komga_sync_queue(session_factory, *, max_jobs: int | None = None) -> int:
    """Drain queued ``komga_sync`` jobs.

    Each job triggers a library scan, then - when the job carries an
    ``issue_id`` (TASK-1327) - maps the publication to its Komga series
    (once, cached forever in ``komga_series_id``) and pushes series +
    book metadata and the cover.

    A no-op (returns 0 without touching the DB or the network) whenever
    ``KOMGA_ENABLED`` is off - an instance without Komga configured must
    see zero difference in behaviour. A Komga failure is recorded as a
    job error and never touches issue status, which already finished (as
    ``done``) when the download job completed.
    """
    settings = resolve_komga_settings(session_factory)
    if not settings["enabled"] or not settings["url"]:
        return 0

    wait_seconds = _komga_wait_seconds()
    processed = 0
    while max_jobs is None or processed < max_jobs:
        with get_session(session_factory) as session:
            repo = DownloadRepository(session)
            job = repo.get_oldest_queued_job("komga_sync")
            if job is None:
                break
            job_id: int = job.id
            try:
                payload = json.loads(job.payload)
            except (TypeError, ValueError):
                payload = {}
            library_id = payload.get("library_id") or settings["library_id"]
            issue_id = payload.get("issue_id")
            repo.start_job(job_id)

        if not library_id:
            with get_session(session_factory) as s2:
                DownloadRepository(s2).finish_job(
                    job_id, error="No Komga library configured"
                )
            logger.error("Komga sync job %d failed: no library configured", job_id)
            processed += 1
            continue

        client = KomgaClient(
            settings["url"],
            username=settings["username"],
            password=settings["password"],
            api_key=settings["api_key"],
        )
        try:
            client.scan_library(library_id)
        except KomgaError as exc:
            with get_session(session_factory) as s2:
                DownloadRepository(s2).finish_job(job_id, error=str(exc))
            logger.error("Komga sync job %d failed: %s", job_id, exc)
            processed += 1
            continue

        sync_error = None
        if issue_id is not None:
            try:
                sync_error = _push_publication_and_issue_metadata(
                    session_factory, client, library_id, int(issue_id), wait_seconds
                )
            except KomgaError as exc:
                sync_error = str(exc)
            if sync_error:
                logger.error("Komga sync job %d failed: %s", job_id, sync_error)

        with get_session(session_factory) as s2:
            DownloadRepository(s2).finish_job(job_id, error=sync_error)
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

    scheduler.add_job(
        run_komga_sync_queue,
        trigger="interval",
        seconds=download_interval_seconds,
        id="komga_sync",
        kwargs=dict(session_factory=session_factory),
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
