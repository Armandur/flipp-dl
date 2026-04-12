"""Index issues.status and issues.publication_id.

Both columns are filtered on by the repository (``list_issues``,
``mark_issue_*``) and by the scheduler when scanning for queued jobs.
SQLite does not auto-create an index for foreign keys, so we add both
explicitly.

Revision ID: 0002_issue_indexes
Revises: 0001_baseline
Create Date: 2026-04-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_issue_indexes"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_issues_publication_id",
        "issues",
        ["publication_id"],
    )
    op.create_index(
        "ix_issues_status",
        "issues",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_issues_status", table_name="issues")
    op.drop_index("ix_issues_publication_id", table_name="issues")
