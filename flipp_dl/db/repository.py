"""Data-access layer for flipp-dl.

:class:`DownloadRepository` is the single point of contact between the
application logic (downloader, scheduler, web UI) and the database.  It
accepts a :class:`sqlalchemy.orm.Session` so callers control transaction
boundaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session, selectinload

from .. import storage
from ..models import Issue as DomainIssue
from ..models import Publication as DomainPublication
from .models import (
    DbIssue,
    DbJob,
    DbPublication,
    DbPublicationCategory,
    DbSetting,
    IssueStatus,
    JobStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _issue_identity(issue: DbIssue) -> dict:
    """A compact, JSON-friendly identity for *issue* used in reports."""
    return {
        "issue_id": issue.id,
        "publication": issue.publication.name if issue.publication else None,
        "issue_name": issue.issue_name,
    }


@dataclass
class ImportReport:
    """Result of reconciling on-disk files against the issues table.

    Produced by :meth:`DownloadRepository.import_existing_files`
    (TASK-1283). Covers both directions of drift a manual sweep of the
    production instance found on 2026-08-18: an issue that sat on disk
    while the DB still called it ``queued``, and two issues claiming the
    same file - one of which was therefore never really downloaded.
    """

    backfilled: list[dict] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    missing_files: list[dict] = field(default_factory=list)
    shared_files: list[dict] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.orphan_files or self.missing_files or self.shared_files)


class DownloadRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Publications
    # ------------------------------------------------------------------

    def upsert_publication(self, pub: DomainPublication) -> DbPublication:
        """Insert or update a publication and its categories.

        Returns the persisted :class:`DbPublication` (not yet committed).
        """
        db_pub = self.session.scalar(
            select(DbPublication).where(DbPublication.custom_code == pub.custom_code)
        )
        if db_pub is None:
            db_pub = DbPublication(
                custom_code=pub.custom_code,
                name=pub.name,
                cover_url=pub.cover_url,
                description=pub.description,
                next_issue_date=pub.next_issue_date,
            )
            self.session.add(db_pub)
            self.session.flush()  # get id
        else:
            db_pub.name = pub.name
            if pub.cover_url:
                db_pub.cover_url = pub.cover_url
            # Description / next_issue_date are allowed to be cleared
            # on a later poll so always assign.
            db_pub.description = pub.description
            db_pub.next_issue_date = pub.next_issue_date

        # Sync categories (replace all)
        for cat_row in db_pub.categories:
            self.session.delete(cat_row)
        self.session.flush()
        for cat in pub.categories:
            self.session.add(
                DbPublicationCategory(
                    publication_id=db_pub.id,
                    category_id=cat.id,
                    category_name=cat.name,
                )
            )
        return db_pub

    def get_publication(self, custom_code: str) -> DbPublication | None:
        return self.session.scalar(
            select(DbPublication)
            .options(
                selectinload(DbPublication.categories),
                selectinload(DbPublication.issues),
            )
            .where(DbPublication.custom_code == custom_code)
        )

    def list_publications(self, watched_only: bool = False) -> list[DbPublication]:
        """Return publications with issue counts, without loading issues.

        The list view only ever renders ``num_issues``/``num_downloaded``
        per row - selectinload-ing the whole ``issues`` relationship used
        to drag in every issue in the database (17660 rows on the
        production instance) just to compute two integers (TASK-1338).
        The counts are aggregated in the DB instead and handed to each
        row via the ``num_issues``/``num_downloaded`` setters.
        """
        q = select(DbPublication).options(selectinload(DbPublication.categories))
        if watched_only:
            q = q.where(DbPublication.watched == True)  # noqa: E712
        pubs = list(self.session.scalars(q))
        counts = self._issue_counts_by_publication()
        for pub in pubs:
            total, done = counts.get(pub.id, (0, 0))
            pub.num_issues = total
            pub.num_downloaded = done
        return pubs

    def _issue_counts_by_publication(self) -> dict[int, tuple[int, int]]:
        """Return ``{publication_id: (total_issues, done_issues)}``.

        One grouped query over the issues table instead of one row per
        issue - see :meth:`list_publications`.
        """
        rows = self.session.execute(
            select(
                DbIssue.publication_id,
                func.count(DbIssue.id),
                func.sum(case((DbIssue.status == IssueStatus.DONE, 1), else_=0)),
            ).group_by(DbIssue.publication_id)
        ).all()
        return {
            publication_id: (int(total), int(done or 0))
            for publication_id, total, done in rows
        }

    def set_watched(self, custom_code: str, enabled: bool) -> bool:
        """Toggle the watch flag. Returns False if publication not found."""
        db_pub = self.get_publication(custom_code)
        if db_pub is None:
            return False
        db_pub.watched = enabled
        return True

    def mark_polled(self, custom_code: str) -> None:
        db_pub = self.get_publication(custom_code)
        if db_pub:
            db_pub.last_polled_at = _now()

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def upsert_issue(
        self, issue: DomainIssue, publication_id: int
    ) -> tuple[DbIssue, bool]:
        """Insert a new issue or return the existing one.

        Returns ``(db_issue, created)`` where *created* is True for new rows.
        """
        db_issue = self.session.scalar(
            select(DbIssue).where(
                DbIssue.publication_id == publication_id,
                DbIssue.custom_code == issue.custom_code,
            )
        )
        if db_issue is not None:
            return db_issue, False

        db_issue = DbIssue(
            publication_id=publication_id,
            custom_code=issue.custom_code,
            issue_name=issue.issue_name,
            issue_date=issue.issue_date,
            status=IssueStatus.NEW,
        )
        self.session.add(db_issue)
        self.session.flush()
        return db_issue, True

    def get_issue(self, issue_id: int) -> DbIssue | None:
        return self.session.get(DbIssue, issue_id)

    def get_issue_by_code(
        self, custom_code: str, publication_id: int
    ) -> DbIssue | None:
        return self.session.scalar(
            select(DbIssue).where(
                DbIssue.custom_code == custom_code,
                DbIssue.publication_id == publication_id,
            )
        )

    def get_issue_by_file_path(self, file_path: str) -> DbIssue | None:
        """Return the issue that owns *file_path*, if any."""
        return self.session.scalar(
            select(DbIssue).where(DbIssue.file_path == str(file_path))
        )

    def list_issues_sharing_files(self) -> list[list[DbIssue]]:
        """Group issues that claim the same file on disk.

        Two issues pointing at one file means one of them was never
        actually downloaded - its content is nowhere (TASK-1349).
        """
        by_path: dict[str, list[DbIssue]] = {}
        rows = self.session.scalars(
            select(DbIssue)
            .options(selectinload(DbIssue.publication))
            .where(DbIssue.file_path.is_not(None))
        )
        for issue in rows:
            by_path.setdefault(issue.file_path, []).append(issue)
        return [group for group in by_path.values() if len(group) > 1]

    def import_existing_files(self, output_root: Path) -> ImportReport:
        """Reconcile issue status with what is actually on disk.

        Read-only on the filesystem - only the DB is written. A file on
        disk whose expected name matches an issue that isn't ``done``
        yet backfills that issue (status, ``file_path``,
        ``downloaded_at``) without re-downloading anything. A file only
        ever backfills an issue if it resolves inside *output_root* -
        the same containment check the web layer uses
        (:func:`storage.resolve_safe_path`) - so a crafted or stale path
        can never mark an issue done from outside the managed tree.

        Everything the scan can't cleanly explain is reported rather
        than silently fixed: files with no matching issue (orphans),
        ``done`` issues whose file has disappeared, issues that already
        share one file with another (see :meth:`list_issues_sharing_files`,
        TASK-1349), and a not-yet-done issue whose expected filename is
        already claimed by a *different* ``done`` issue - backfilling
        that one blindly would recreate exactly the same bug instead of
        catching it.
        """
        try:
            root = Path(output_root).resolve()
        except OSError:
            root = Path(output_root)

        issues = sorted(
            self.session.scalars(
                select(DbIssue).options(selectinload(DbIssue.publication))
            ),
            key=lambda i: i.id,
        )

        # Every path a real download could have produced for this issue:
        # the plain name and the disambiguated form two issues get when
        # they'd otherwise collide (TASK-1349). Lowest id wins a clash
        # on the plain form so the result is deterministic.
        by_path: dict[Path, DbIssue] = {}
        for issue in issues:
            pub = issue.publication
            if pub is None:
                continue
            for disambiguate in (False, True):
                path = storage.issue_path(root, pub, issue, disambiguate=disambiguate)
                by_path.setdefault(path, issue)

        report = ImportReport()

        if root.is_dir():
            for pdf in sorted(root.rglob("*.pdf")):
                if not pdf.is_file():
                    continue
                try:
                    resolved = pdf.resolve()
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue  # escapes output_root via a symlink - ignore
                issue = by_path.get(resolved)
                if issue is None:
                    report.orphan_files.append(str(resolved.relative_to(root)))
                    continue
                if issue.status == IssueStatus.DONE:
                    continue

                # Another issue may already have this exact path recorded
                # as its file_path - e.g. it was the one actually
                # downloaded when two issues collided on the plain name
                # (TASK-1349). Backfilling *this* issue on top of it would
                # silently recreate that bug, so report it instead.
                owner = self.get_issue_by_file_path(str(resolved))
                if owner is not None and owner.id != issue.id:
                    report.shared_files.append(
                        {
                            "file_path": str(resolved),
                            "issues": [
                                {**_issue_identity(owner), "status": owner.status},
                                {**_issue_identity(issue), "status": issue.status},
                            ],
                        }
                    )
                    continue

                self.mark_issue_done(issue.id, str(resolved))
                report.backfilled.append(
                    {**_issue_identity(issue), "file_path": str(resolved)}
                )

        for issue in issues:
            if issue.status != IssueStatus.DONE or not issue.file_path:
                continue
            if storage.resolve_safe_path(root, issue.file_path) is None:
                report.missing_files.append(
                    {**_issue_identity(issue), "file_path": issue.file_path}
                )

        for group in self.list_issues_sharing_files():
            report.shared_files.append(
                {
                    "file_path": group[0].file_path,
                    "issues": [
                        {**_issue_identity(issue), "status": issue.status}
                        for issue in group
                    ],
                }
            )

        return report

    def list_issues(
        self,
        publication_id: int | None = None,
        status: str | None = None,
    ) -> list[DbIssue]:
        q = select(DbIssue).options(selectinload(DbIssue.publication))
        if publication_id is not None:
            q = q.where(DbIssue.publication_id == publication_id)
        if status is not None:
            q = q.where(DbIssue.status == status)
        return list(self.session.scalars(q))

    def count_issues_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` over the issues table.

        Counting in the DB instead of loading every row matters on a
        real instance - the dashboard polls this and there are tens of
        thousands of issues.
        """
        rows = self.session.execute(
            select(DbIssue.status, func.count(DbIssue.id)).group_by(DbIssue.status)
        ).all()
        counts = {
            IssueStatus.NEW.value: 0,
            IssueStatus.QUEUED.value: 0,
            IssueStatus.DOWNLOADING.value: 0,
            IssueStatus.DONE.value: 0,
            IssueStatus.ERROR.value: 0,
        }
        for status, count in rows:
            counts[status] = int(count)
        return counts

    def count_publications(self) -> tuple[int, int]:
        """Return ``(total, watched)`` publication counts."""
        total = int(self.session.scalar(select(func.count(DbPublication.id))) or 0)
        watched = int(
            self.session.scalar(
                select(func.count(DbPublication.id)).where(
                    DbPublication.watched == True  # noqa: E712
                )
            )
            or 0
        )
        return total, watched

    def list_recent_downloads(self, limit: int = 10) -> list[DbIssue]:
        """Return the most recently downloaded issues, newest first."""
        q = (
            select(DbIssue)
            .options(selectinload(DbIssue.publication))
            .where(DbIssue.status == IssueStatus.DONE)
            .order_by(DbIssue.downloaded_at.desc(), DbIssue.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(q))

    def get_issues_by_ids(self, issue_ids: list[int]) -> dict[int, DbIssue]:
        """Return ``{issue_id: DbIssue}`` for the given ids, publication loaded.

        One query for the whole page - the jobs list would otherwise do a
        lookup per row just to name what each job is about.
        """
        if not issue_ids:
            return {}
        rows = self.session.scalars(
            select(DbIssue)
            .options(selectinload(DbIssue.publication))
            .where(DbIssue.id.in_(set(issue_ids)))
        )
        return {issue.id: issue for issue in rows}

    def mark_issue_queued(self, issue_id: int) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.QUEUED

    def mark_issue_downloading(self, issue_id: int) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.DOWNLOADING
            issue.progress_current = 0
            issue.progress_total = 0

    def update_issue_progress(self, issue_id: int, current: int, total: int) -> None:
        """Record live page-download progress for *issue_id*.

        Called by the downloader after each page completes so the UI can
        poll and render ``current / total pages``. Safe to call with
        ``total=0`` to clear the counters.
        """
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.progress_current = current
            issue.progress_total = total

    def mark_issue_done(self, issue_id: int, file_path: str) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.DONE
            issue.file_path = file_path
            issue.downloaded_at = _now()
            issue.error_message = None
            issue.progress_current = 0
            issue.progress_total = 0

    def mark_issue_error(self, issue_id: int, error: str) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.ERROR
            issue.error_message = error
            issue.progress_current = 0
            issue.progress_total = 0

    def reset_issue(self, issue_id: int) -> None:
        """Clear download metadata so the issue is treated as not downloaded.

        Does not touch the file on disk – callers are expected to unlink
        the file (if desired) before calling this.
        """
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.NEW
            issue.file_path = None
            issue.downloaded_at = None
            issue.error_message = None

    # ------------------------------------------------------------------
    # Sync helper – call after a fresh API fetch
    # ------------------------------------------------------------------

    def sync_publications(
        self, api_publications: list[DomainPublication]
    ) -> list[DbIssue]:
        """Upsert all publications + issues from a fresh API response.

        Returns a list of *newly discovered* :class:`DbIssue` rows that
        the caller can queue for download.
        """
        new_issues: list[DbIssue] = []
        for pub in api_publications:
            db_pub = self.upsert_publication(pub)
            db_pub.last_polled_at = _now()
            for issue in pub.issues:
                db_issue, created = self.upsert_issue(issue, db_pub.id)
                if created:
                    new_issues.append(db_issue)
        return new_issues

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.session.get(DbSetting, key)
        return row.value if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        row = self.session.get(DbSetting, key)
        if row is None:
            self.session.add(DbSetting(key=key, value=value))
        else:
            row.value = value

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def create_job(self, job_type: str, payload: dict | None = None) -> DbJob:
        job = DbJob(
            job_type=job_type,
            payload=json.dumps(payload or {}),
            status=JobStatus.QUEUED,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def start_job(self, job_id: int) -> None:
        job = self.session.get(DbJob, job_id)
        if job:
            job.status = JobStatus.RUNNING
            job.started_at = _now()

    def finish_job(self, job_id: int, error: str | None = None) -> None:
        job = self.session.get(DbJob, job_id)
        if job:
            job.status = JobStatus.ERROR if error else JobStatus.DONE
            job.finished_at = _now()
            job.error_message = error

    def list_jobs(
        self,
        limit: int = 50,
        status: str | None = None,
        job_type: str | None = None,
    ) -> list[DbJob]:
        """Return the newest jobs, optionally narrowed by status/type.

        Filtering happens in the query, not on the returned page - a
        queue deeper than *limit* would otherwise be invisible behind
        newer finished jobs.
        """
        q = select(DbJob)
        if status is not None:
            q = q.where(DbJob.status == status)
        if job_type is not None:
            q = q.where(DbJob.job_type == job_type)
        q = q.order_by(DbJob.created_at.desc(), DbJob.id.desc()).limit(limit)
        return list(self.session.scalars(q))

    def count_jobs_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` over the whole jobs table.

        Every known status is present in the result, zero-filled, so
        callers can render a counter without guarding for missing keys.
        """
        rows = self.session.execute(
            select(DbJob.status, func.count(DbJob.id)).group_by(DbJob.status)
        ).all()
        counts = {
            JobStatus.QUEUED.value: 0,
            JobStatus.RUNNING.value: 0,
            JobStatus.DONE.value: 0,
            JobStatus.ERROR.value: 0,
        }
        for status, count in rows:
            counts[status] = int(count)
        return counts

    def get_job(self, job_id: int) -> DbJob | None:
        return self.session.get(DbJob, job_id)

    def get_oldest_queued_job(self, job_type: str) -> DbJob | None:
        """Return the oldest queued job of *job_type*, or ``None``.

        Queries the DB directly for the oldest match instead of
        Python-filtering a fixed-size window of recent jobs – a
        bulk-queue that pushes more than the window size of newer jobs
        would otherwise permanently hide an older queued job (TASK-1282).

        ``created_at`` isn't guaranteed millisecond resolution on
        SQLite, so ``id`` is used as a deterministic tiebreaker.
        """
        stmt = (
            select(DbJob)
            .where(DbJob.job_type == job_type, DbJob.status == JobStatus.QUEUED)
            .order_by(DbJob.created_at.asc(), DbJob.id.asc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def reset_stuck_download_jobs(self) -> int:
        """Reset RUNNING download jobs (and their issues) back to QUEUED.

        If the process crashes or is restarted mid-download, the job row
        stays RUNNING and the issue stays DOWNLOADING forever – nothing
        ever picks it up again, and the web UI's manual re-queue button
        skips issues in that state. Called once at startup.

        Returns the number of job rows reset.
        """
        stmt = select(DbJob).where(
            DbJob.job_type == "download", DbJob.status == JobStatus.RUNNING
        )
        stuck_jobs = list(self.session.scalars(stmt))
        for job in stuck_jobs:
            job.status = JobStatus.QUEUED
            job.started_at = None
            try:
                payload = json.loads(job.payload)
            except (TypeError, ValueError):
                payload = {}
            issue_id = payload.get("issue_id")
            if issue_id is not None:
                issue = self.session.get(DbIssue, int(issue_id))
                if issue is not None and issue.status == IssueStatus.DOWNLOADING:
                    issue.status = IssueStatus.QUEUED
        return len(stuck_jobs)

    def list_active_download_jobs(self) -> list[DbJob]:
        """Download jobs that are still queued or running."""
        return list(
            self.session.scalars(
                select(DbJob).where(
                    DbJob.job_type == "download",
                    DbJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                )
            )
        )

    def release_duplicate_file_claims(self) -> int:
        """Un-mark issues that claim a file belonging to another issue.

        The oldest download keeps the file; the others never had one of
        their own, so they go back to not-downloaded and get picked up
        by the next backfill (TASK-1349).

        Returns the number of issues released.
        """
        released = 0
        for group in self.list_issues_sharing_files():
            # Whoever downloaded first owns the file; fall back to the
            # lowest id when timestamps are missing or equal.
            keeper = min(
                group,
                key=lambda i: (i.downloaded_at or datetime.max, i.id),
            )
            for issue in group:
                if issue.id == keeper.id:
                    continue
                issue.status = IssueStatus.NEW
                issue.file_path = None
                issue.downloaded_at = None
                issue.progress_current = 0
                issue.progress_total = 0
                released += 1
        return released

    def reset_orphaned_issues(self) -> int:
        """Reset issues stuck in queued/downloading with no job behind them.

        Issue status and the jobs table can drift apart - a restart at
        the wrong moment, or a job that died after the issue was marked
        queued. The row then sits there forever: nothing picks it up,
        and the UI refuses to re-queue an issue that already claims to
        be queued (TASK-1341).

        Returns the number of issues reset.
        """
        active_issue_ids: set[int] = set()
        for job in self.list_active_download_jobs():
            try:
                payload = json.loads(job.payload or "{}")
            except (TypeError, ValueError):
                continue
            issue_id = payload.get("issue_id")
            if issue_id is not None:
                try:
                    active_issue_ids.add(int(issue_id))
                except (TypeError, ValueError):
                    continue

        stuck = self.session.scalars(
            select(DbIssue).where(
                DbIssue.status.in_((IssueStatus.QUEUED, IssueStatus.DOWNLOADING))
            )
        )
        reset = 0
        for issue in stuck:
            if issue.id in active_issue_ids:
                continue
            issue.status = IssueStatus.NEW
            issue.progress_current = 0
            issue.progress_total = 0
            reset += 1
        return reset

    def queue_missing_issues(
        self, publication_id: int, *, include_failed: bool = True
    ) -> int:
        """Queue every issue of a publication that isn't downloaded yet.

        Watching a publication used to queue nothing, and a poll only
        queues newly discovered issues - so everything already in the
        database when watching was turned on never downloaded at all
        (TASK-1346).

        Issues already queued or downloading are skipped, so calling
        this repeatedly is safe. *include_failed* re-queues issues that
        previously errored: right for an explicit click, wrong for an
        automatic poll, where a permanently broken issue would come
        back every six hours.

        Returns the number of issues queued.
        """
        wanted = [IssueStatus.NEW]
        if include_failed:
            wanted.append(IssueStatus.ERROR)

        pending = self.session.scalars(
            select(DbIssue).where(
                DbIssue.publication_id == publication_id,
                DbIssue.status.in_(wanted),
            )
        )
        queued = 0
        for issue in pending:
            self.mark_issue_queued(issue.id)
            self.create_job("download", {"issue_id": issue.id})
            queued += 1
        return queued

    def cancel_issue(self, issue_id: int) -> int:
        """Stop an in-flight issue: reset it and finish its jobs.

        Returns the number of jobs that were marked cancelled. Used by
        the UI so a stuck row always has a way out.
        """
        cancelled = 0
        for job in self.list_active_download_jobs():
            try:
                payload = json.loads(job.payload or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("issue_id") == issue_id:
                self.finish_job(job.id, error="Cancelled from the web UI")
                cancelled += 1

        issue = self.session.get(DbIssue, issue_id)
        if issue is not None:
            issue.status = IssueStatus.NEW
            issue.progress_current = 0
            issue.progress_total = 0
        return cancelled

    def purge_old_jobs(
        self,
        *,
        max_age_days: int = 30,
        keep_min: int = 500,
    ) -> int:
        """Delete finished jobs older than *max_age_days*.

        The newest *keep_min* finished jobs are always retained so a
        busy instance still has recent history for the Jobs page even
        if it just finished a big bulk-download. Running or queued
        jobs are never purged – they either finish and become eligible
        later, or they stay forever if the process crashed with stuck
        rows (which is the user's cue to investigate).

        Returns the number of rows removed.
        """
        terminal = (JobStatus.DONE, JobStatus.ERROR)
        cutoff = _now() - timedelta(days=max_age_days)

        keep_ids: set[int] = set()
        if keep_min > 0:
            keep_ids = set(
                self.session.scalars(
                    select(DbJob.id)
                    .where(DbJob.status.in_(terminal))
                    .order_by(DbJob.created_at.desc())
                    .limit(keep_min)
                )
            )

        stmt = delete(DbJob).where(
            DbJob.status.in_(terminal),
            DbJob.created_at < cutoff,
        )
        if keep_ids:
            stmt = stmt.where(~DbJob.id.in_(keep_ids))

        result = self.session.execute(stmt)
        return int(result.rowcount or 0)
