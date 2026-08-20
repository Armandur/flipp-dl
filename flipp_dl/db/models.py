"""SQLAlchemy ORM models for flipp-dl."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Issue status lifecycle: new → queued → downloading → done | error
# ---------------------------------------------------------------------------


class IssueStatus(str, Enum):
    NEW = "new"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DONE = "done"
    ERROR = "error"
    # Failed with a transient-looking error and still has automatic
    # retries left (TASK-1363). Deliberately distinct from QUEUED: a
    # RETRY_PENDING issue is not eligible for
    # ``get_oldest_queued_job``/reset_orphaned_issues (no job exists for
    # it yet, and it must not look like a stuck queued row on startup),
    # and the "Retry"/"Download" buttons must be able to tell "waiting
    # for its own backoff window" apart from "actually in the queue
    # right now". See DownloadRepository.schedule_issue_retry.
    RETRY_PENDING = "retry_pending"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Publications
# ---------------------------------------------------------------------------


class DbPublication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    custom_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Direct cover-art URL as returned by the Flipp API
    # (``latestCoverImageUrl``). Nullable because older rows predate this
    # column and the API may omit it for some publications.
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Filename of the locally cached copy of ``cover_url``, relative to
    # ``default_cover_cache_root()`` (TASK-1345). ``None`` until the poll
    # tick that discovers/refreshes this publication has fetched it -
    # the template falls back to the missing-cover placeholder until then.
    cover_cache_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # The ``cover_url`` value that was last successfully cached, so a poll
    # tick only re-downloads when Flipp actually published a new cover
    # instead of hitting the network every six hours for nothing.
    cover_cache_source_url: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    # HTML blurb returned by Flipp in the ``description`` field. Kept raw
    # so the detail page can render it – sanitised before output.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Parsed release date of the next issue (YYYY-MM-DD) if the
    # description contains the standard "Nästa nummer kommer …" line.
    next_issue_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    watched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When watching was (most recently) turned on for this publication
    # (TASK-1361). Watching only bevakar framåt - it queues nothing by
    # itself - so poll's catch-up pass uses this as the cutoff: an issue
    # discovered before this timestamp is part of the back catalogue and
    # is left alone; only issues discovered from here onward are ever
    # auto-queued. Reset every time watching is (re-)enabled, including
    # after an unwatch/watch cycle - re-watching must not silently pull
    # in whatever accumulated while unwatched either.
    watch_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Per-publication poll interval override, in minutes (TASK-1291). None
    # means "use the global default" - today's behaviour, queued/backfilled
    # on every poll tick. Set only when the user wants a slower cadence
    # than the global default (e.g. a monthly magazine).
    poll_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Bookkeeping for the override above: when this publication is next
    # allowed to have its new issues queued/backfilled. Distinct from
    # ``last_polled_at``, which ``sync_publications()`` stamps on every
    # publication every tick regardless of any override - reusing it here
    # would make the override reset itself on every poll.
    next_poll_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Komga series id this publication maps to (TASK-1327), resolved lazily
    # on the first successful sync and cached here forever afterwards -
    # the folder-name lookup against Komga's search endpoint is never
    # repeated once a match has been found.
    komga_series_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    issues: Mapped[list[DbIssue]] = relationship(
        "DbIssue", back_populates="publication", cascade="all, delete-orphan"
    )
    categories: Mapped[list[DbPublicationCategory]] = relationship(
        "DbPublicationCategory",
        back_populates="publication",
        cascade="all, delete-orphan",
    )

    @property
    def num_issues(self) -> int:
        override = getattr(self, "_num_issues_override", None)
        if override is not None:
            return override
        return len(self.issues)

    @num_issues.setter
    def num_issues(self, value: int) -> None:
        """Let the repository hand in a pre-aggregated count (TASK-1338).

        ``list_publications()`` no longer loads ``issues`` for every row,
        so this lets it report the DB-side count without falling back to
        the (now unloaded) relationship.
        """
        self._num_issues_override = value

    @property
    def num_downloaded(self) -> int:
        override = getattr(self, "_num_downloaded_override", None)
        if override is not None:
            return override
        return sum(1 for issue in self.issues if issue.status == IssueStatus.DONE)

    @num_downloaded.setter
    def num_downloaded(self, value: int) -> None:
        self._num_downloaded_override = value

    def __repr__(self) -> str:
        return f"<Publication {self.custom_code!r} watched={self.watched}>"


class DbPublicationCategory(Base):
    """Denormalised category rows attached to each publication."""

    __tablename__ = "publication_categories"
    __table_args__ = (UniqueConstraint("publication_id", "category_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("publications.id"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(Integer, nullable=False)
    category_name: Mapped[str] = mapped_column(String(100), nullable=False)

    publication: Mapped[DbPublication] = relationship(
        "DbPublication", back_populates="categories"
    )


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


class DbIssue(Base):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("publication_id", "custom_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("publications.id"),
        nullable=False,
        index=True,
    )
    custom_code: Mapped[str] = mapped_column(String(100), nullable=False)
    issue_name: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_date: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=IssueStatus.NEW, nullable=False, index=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Size in bytes of the merged PDF at ``file_path``, captured once when
    # the issue is marked done (download or disk-import backfill). Stored
    # rather than stat()'d on demand - the size-estimate feature
    # (TASK-1362) reads this column for hundreds of "done" issues on every
    # bulk-queue confirm; re-statting that many files from disk on every
    # page render would be the expensive path this avoids. ``None`` for
    # issues downloaded before this column existed.
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Number of automatic retries already used after a transient download
    # failure (TASK-1363). Reset to 0 whenever the issue is (re-)queued
    # through the normal path (manual click, bulk backfill) - only the
    # automatic backoff requeue leaves it alone, since that is exactly
    # the counter it exists to enforce a ceiling on.
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # When a RETRY_PENDING issue becomes eligible to be requeued again.
    # None once the issue is queued/downloading/done, or once it has
    # given up for good (status stays ERROR and this is left as-is).
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Live progress for the active download – updated after each page is
    # fetched so the web UI can poll and render "3 / 12 pages". Both
    # columns are 0 when no download is in flight.
    progress_current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Filename of the locally cached cover thumbnail for this issue,
    # relative to ``default_cover_cache_root()`` (TASK-1345). Fetched once
    # when the issue is first discovered by a poll - an issue's cover
    # never changes afterwards, so there is nothing to invalidate.
    cover_cache_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Komga book id this issue maps to (TASK-1328), captured the first
    # time the nivå-2 metadata push (:mod:`flipp_dl.scheduler`) matches a
    # book by filename stem. Needed to ask Komga for read progress
    # without redoing that lookup. ``None`` until pushed at least once.
    komga_book_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Locally cached read status from Komga (TASK-1328), refreshed once a
    # day by :func:`flipp_dl.scheduler.run_komga_read_status_sync` - never
    # queried on page load. ``None`` means "not synced yet" (or no book
    # mapped at all); the template must not render a badge for ``None``.
    komga_read: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    komga_read_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    komga_read_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    publication: Mapped[DbPublication] = relationship(
        "DbPublication", back_populates="issues"
    )

    def __repr__(self) -> str:
        return f"<Issue {self.custom_code!r} status={self.status}>"


# ---------------------------------------------------------------------------
# Settings (simple key-value store)
# ---------------------------------------------------------------------------


class DbSetting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Jobs (audit log for background tasks)
# ---------------------------------------------------------------------------


class DbJob(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # poll | download
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON blob
    status: Mapped[str] = mapped_column(
        String(20), default=JobStatus.QUEUED, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Job #{self.id} {self.job_type} status={self.status}>"
