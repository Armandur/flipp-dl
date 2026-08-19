"""Add Komga book mapping and cached read status to issues.

Komga nivå 3 (TASK-1328) shows lästa/olästa utgåvor without asking Komga
on every page load, which needs two things persisted on ``issues``:

- ``komga_book_id`` - the Komga book id this issue maps to. Nivå 2
  (TASK-1327) finds this on every sync but never kept it; nivå 3 needs it
  to ask Komga about one specific book's read progress without redoing
  the filename-stem lookup every time.
- ``komga_read`` / ``komga_read_page`` / ``komga_read_synced_at`` - the
  cached read status, refreshed once a day by a scheduled job
  (:func:`flipp_dl.scheduler.run_komga_read_status_sync`). ``komga_read``
  is ``NULL`` until the first successful sync (or when the issue has no
  book mapped at all) - the template must not render a badge for that.

Revision ID: 0008_komga_read_status
Revises: 0007_komga_series_id
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_komga_read_status"
down_revision: str | None = "0007_komga_series_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("komga_book_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("komga_read", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("komga_read_page", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("komga_read_synced_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("komga_read_synced_at")
        batch_op.drop_column("komga_read_page")
        batch_op.drop_column("komga_read")
        batch_op.drop_column("komga_book_id")
