"""Add retry_count/next_retry_at to issues.

TASK-1363: a failed download used to stay ``error`` forever - the only
way back was a manual click, and poll's catch-up pass deliberately
excludes ``error`` issues (a permanently broken issue would otherwise be
requeued every six hours). Transient failures (a network blip, a locked
database, a 5xx from Flipp) therefore never healed on their own; the
production instance had exactly such a job stuck since June, failed on
"database is locked" long after the underlying cause was fixed.

``retry_count`` and ``next_retry_at`` back the new ``retry_pending``
issue status (see ``flipp_dl.db.models.IssueStatus``): a failed download
that still has automatic attempts left moves there with a growing
backoff instead of straight to ``error``, and
``DownloadRepository.requeue_due_retries`` promotes it back to
``queued`` once its own ``next_retry_at`` has passed.

Both columns default to "no retry in flight" for every existing row, so
issues already sitting as ``error`` are entirely unaffected - they stay
``error`` and still require a deliberate click, exactly as before.

Revision ID: 0011_issue_retry
Revises: 0010_watch_started_at
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_issue_retry"
down_revision: str | None = "0010_watch_started_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("next_retry_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("next_retry_at")
        batch_op.drop_column("retry_count")
