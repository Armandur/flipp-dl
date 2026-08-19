"""Index jobs.status and jobs.job_type.

The scheduler queries the oldest queued job of a given type directly
(``DownloadRepository.get_oldest_queued_job``) instead of scanning a
fixed-size window in Python – see TASK-1282. Without indexes that query
degrades to a full table scan as the jobs table grows.

Revision ID: 0004_job_indexes
Revises: 0003_issue_progress
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_job_indexes"
down_revision: str | None = "0003_issue_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_jobs_job_type",
        "jobs",
        ["job_type"],
    )
    op.create_index(
        "ix_jobs_status",
        "jobs",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_job_type", table_name="jobs")
