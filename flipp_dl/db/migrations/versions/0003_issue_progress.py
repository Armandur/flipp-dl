"""Add progress_current / progress_total columns to issues.

Used by the downloader to report live page-download progress so the
web UI can poll and render "3 / 12 pages" while a job is in flight.

Revision ID: 0003_issue_progress
Revises: 0002_issue_indexes
Create Date: 2026-04-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_issue_progress"
down_revision: str | None = "0002_issue_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(
            sa.Column(
                "progress_current",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "progress_total",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("progress_total")
        batch_op.drop_column("progress_current")
