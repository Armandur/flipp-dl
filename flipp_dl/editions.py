"""Shared logic for discovering and importing editions via PageSuite.

The CLI (``--discover-editions``, ``--import-editions``) and the web UI's
background jobs both run these, so the network calls and the database
writes live here. Both are long-running - a discovery run makes one HTTP
call per publication - so each takes an optional ``on_progress`` callback
that the job runner uses to write progress back to its job row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

# ``pagesuite`` is imported as a module, not as names: the client class is
# looked up at call time so a test (or anything else) can swap it out after
# this module has been imported.
from . import pagesuite
from .codes import CodeFileError
from .db.repository import DownloadRepository
from .db.session import get_session
from .models import Issue
from .pagesuite import PageSuiteClient, PageSuiteError

logger = logging.getLogger(__name__)

# The key the unlisted-editions file wraps its list in - the shape written
# by the exploration scripts (``docs/olistade-utgavor.json``). A bare list
# is accepted too.
EDITIONS_KEY = "found_unlisted_issues"

ProgressCallback = Callable[[int, int, int], None]
"""``(done, total, new)`` - called as the run advances."""


def parse_editions(data: bytes | str) -> list:
    """Parse an editions file: a JSON list, or an object wrapping one."""
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CodeFileError("unreadable", str(exc)) from exc
    if isinstance(parsed, dict):
        parsed = parsed.get(EDITIONS_KEY, [])
    if not isinstance(parsed, list):
        raise CodeFileError(
            "not_a_list", f"expected a list, or an object with a {EDITIONS_KEY!r} list"
        )
    return parsed


@dataclass
class DiscoveryResult:
    """Counts from one discovery run, plus the publications that grew."""

    checked: int = 0
    failed: int = 0
    new_editions: int = 0
    per_publication: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class EditionImportResult:
    """Counts from importing unlisted editions."""

    imported: int = 0
    dead: int = 0
    no_publication: int = 0
    per_publication: list[tuple[str, int]] = field(default_factory=list)


def discover_all_editions(
    session_factory,
    client: PageSuiteClient | None = None,
    on_progress: ProgressCallback | None = None,
) -> DiscoveryResult:
    """Fetch the full edition list for every publication and store what's new.

    One HTTP call per publication, each in its own short session so a long
    run never holds a database transaction open. A publication PageSuite
    refuses is counted as failed and skipped - one bad pubid must not stop
    the rest of the run.
    """
    client = client or pagesuite.PageSuiteClient()
    with get_session(session_factory) as session:
        pubs = [
            (p.custom_code, p.name)
            for p in DownloadRepository(session).list_publications()
        ]

    result = DiscoveryResult()
    total = len(pubs)
    for done, (code, name) in enumerate(pubs, start=1):
        try:
            editions = client.fetch_editions(code)
        except PageSuiteError as exc:
            logger.warning("Skipped %s: %s", name, exc)
            result.failed += 1
        else:
            result.checked += 1
            with get_session(session_factory) as session:
                repo = DownloadRepository(session)
                db_pub = repo.get_publication(code)
                new_issues = (
                    []
                    if db_pub is None
                    else repo.discover_editions(db_pub.id, editions)
                )
            if new_issues:
                result.new_editions += len(new_issues)
                result.per_publication.append((name, len(new_issues)))
        if on_progress is not None:
            on_progress(done, total, result.new_editions)
    return result


def import_unlisted_editions(
    session_factory,
    entries: list,
    client: PageSuiteClient | None = None,
    on_progress: ProgressCallback | None = None,
) -> EditionImportResult:
    """Import shadow issue codes the listing API never returns.

    Each eid is resolved through the replica API to find its real
    publication and date, then attached to that publication if it exists in
    the database. Editions whose publication is unknown, or whose eid is
    dead, are counted and skipped.
    """
    client = client or pagesuite.PageSuiteClient()
    with get_session(session_factory) as session:
        known_pubs = {
            p.custom_code for p in DownloadRepository(session).list_publications()
        }

    result = EditionImportResult()
    by_pub: dict[str, list[Issue]] = {}
    total = len(entries)
    for done, entry in enumerate(entries, start=1):
        if isinstance(entry, dict):
            _resolve_entry(client, entry, known_pubs, by_pub, result)
        if on_progress is not None:
            # Nothing is written until every eid is resolved, so progress
            # reports what is queued for import, not what is stored yet.
            on_progress(done, total, sum(len(v) for v in by_pub.values()))

    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        for pubid, issues in by_pub.items():
            db_pub = repo.get_publication(pubid)
            if db_pub is None:
                continue
            new_issues = repo.discover_editions(db_pub.id, issues)
            if new_issues:
                result.imported += len(new_issues)
                result.per_publication.append((db_pub.name, len(new_issues)))
    return result


def _resolve_entry(
    client: PageSuiteClient,
    entry: dict,
    known_pubs: set[str],
    by_pub: dict[str, list[Issue]],
    result: EditionImportResult,
) -> None:
    """Look one eid up and file it under its publication, or count why not."""
    eid = entry.get("issue_code")
    if not eid:
        return
    meta = client.fetch_edition_metadata(eid)
    if meta is None:
        result.dead += 1
        return
    pubid = meta.publication_guid or entry.get("publication_code")
    if pubid not in known_pubs:
        result.no_publication += 1
        return
    name = meta.edition_name or entry.get("publication_name") or ""
    by_pub.setdefault(pubid, []).append(
        Issue(custom_code=eid, issue_name=name, issue_date=meta.iso_date)
    )


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

JOB_DISCOVER = "discover_editions"
JOB_IMPORT = "import_editions"
JOB_TYPES = (JOB_DISCOVER, JOB_IMPORT)

# How often progress is written back to the job row. Every step would mean
# one write per HTTP call; every fifth keeps the UI moving without turning
# the run into a write loop.
_PROGRESS_EVERY = 5

# ...and never more than this many writes for one run. An import job carries
# the whole uploaded file in its payload, which is re-serialized on every
# write - without a ceiling a large file would cost O(entries^2).
_PROGRESS_WRITES = 20


def _progress_writer(session_factory, job_id: int) -> ProgressCallback:
    """Return a callback that writes progress into the job's payload."""

    def write(done: int, total: int, found: int) -> None:
        every = max(_PROGRESS_EVERY, total // _PROGRESS_WRITES)
        if done % every and done != total:
            return
        with get_session(session_factory) as session:
            DownloadRepository(session).merge_job_payload(
                job_id, {"progress": {"done": done, "total": total, "found": found}}
            )

    return write


def run_editions_queue(
    session_factory, *, client: PageSuiteClient | None = None, max_jobs: int = 1
) -> int:
    """Drain queued edition discovery/import jobs; return how many ran.

    Both job types are long-running (one HTTP call per publication or per
    edition), so they run here rather than in a request. Progress and the
    final counts are written into the job payload, which is what the
    settings page polls.
    """
    processed = 0
    while processed < max_jobs:
        claimed = _claim_next_editions_job(session_factory)
        if claimed is None:
            break
        job_id, job_type, entries = claimed
        on_progress = _progress_writer(session_factory, job_id)
        error = None
        try:
            if job_type == JOB_DISCOVER:
                result = discover_all_editions(session_factory, client, on_progress)
                summary = {
                    "checked": result.checked,
                    "failed": result.failed,
                    "new_editions": result.new_editions,
                    "per_publication": result.per_publication,
                }
            else:
                result = import_unlisted_editions(
                    session_factory, entries, client, on_progress
                )
                summary = {
                    "imported": result.imported,
                    "dead": result.dead,
                    "no_publication": result.no_publication,
                    "per_publication": result.per_publication,
                }
        except Exception as exc:  # noqa: BLE001 - recorded on the job row
            logger.exception("Editions job %d (%s) failed", job_id, job_type)
            error = str(exc)
            summary = {}
        with get_session(session_factory) as session:
            repo = DownloadRepository(session)
            if summary:
                repo.merge_job_payload(job_id, {"result": summary})
            repo.finish_job(job_id, error=error)
        processed += 1
    return processed


def _claim_next_editions_job(session_factory) -> tuple[int, str, list] | None:
    """Mark the oldest queued editions job RUNNING and return its work.

    Returns ``(job_id, job_type, entries)`` - ``entries`` is empty for a
    discovery run, and the uploaded editions list for an import.
    """
    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        for job_type in JOB_TYPES:
            job = repo.get_oldest_queued_job(job_type)
            if job is None:
                continue
            job_id: int = job.id
            try:
                payload = json.loads(job.payload)
            except (TypeError, ValueError):
                payload = {}
            entries = []
            if job_type == JOB_IMPORT:
                entries = (payload.get("input") or {}).get("entries") or []
                if not entries:
                    repo.finish_job(job_id, error="No editions in the job payload")
                    return None
            repo.start_job(job_id)
            return job_id, job_type, entries
    return None


def reset_stuck_editions_jobs(session_factory) -> int:
    """Requeue editions jobs left RUNNING by a crash or restart."""
    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        return sum(repo.reset_stuck_jobs_of_type(t) for t in JOB_TYPES)
