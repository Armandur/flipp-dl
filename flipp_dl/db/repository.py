"""Data-access layer for flipp-dl.

:class:`DownloadRepository` is the single point of contact between the
application logic (downloader, scheduler, web UI) and the database.  It
accepts a :class:`sqlalchemy.orm.Session` so callers control transaction
boundaries.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
        q = select(DbPublication).options(
            selectinload(DbPublication.categories),
            selectinload(DbPublication.issues),
        )
        if watched_only:
            q = q.where(DbPublication.watched == True)  # noqa: E712
        return list(self.session.scalars(q))

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

    def mark_issue_queued(self, issue_id: int) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.QUEUED

    def mark_issue_downloading(self, issue_id: int) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.DOWNLOADING

    def mark_issue_done(self, issue_id: int, file_path: str) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.DONE
            issue.file_path = file_path
            issue.downloaded_at = _now()
            issue.error_message = None

    def mark_issue_error(self, issue_id: int, error: str) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.ERROR
            issue.error_message = error

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

    def list_jobs(self, limit: int = 50) -> list[DbJob]:
        q = select(DbJob).order_by(DbJob.created_at.desc()).limit(limit)
        return list(self.session.scalars(q))
