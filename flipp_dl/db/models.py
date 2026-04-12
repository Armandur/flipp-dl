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
    watched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    issues: Mapped[list[DbIssue]] = relationship(
        "DbIssue", back_populates="publication", cascade="all, delete-orphan"
    )
    categories: Mapped[list[DbPublicationCategory]] = relationship(
        "DbPublicationCategory",
        back_populates="publication",
        cascade="all, delete-orphan",
    )

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
        Integer, ForeignKey("publications.id"), nullable=False
    )
    custom_code: Mapped[str] = mapped_column(String(100), nullable=False)
    issue_name: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_date: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=IssueStatus.NEW, nullable=False
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)  # poll | download
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON blob
    status: Mapped[str] = mapped_column(
        String(20), default=JobStatus.QUEUED, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Job #{self.id} {self.job_type} status={self.status}>"
