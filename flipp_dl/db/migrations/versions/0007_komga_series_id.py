"""Add Komga series mapping to publications.

Komga nivå 2 (TASK-1327) needs to remember which Komga series a
publication maps to, so the match (folder name -> series id) is only
ever looked up once instead of on every sync:

- ``publications.komga_series_id`` - nullable Komga series id (an
  integer in Komga's own API). ``NULL`` means "not mapped yet - try
  again on the next successful sync", which the detail page renders as
  "Komga: okänd - söker nästa gång".

Revision ID: 0007_komga_series_id
Revises: 0006_publication_poll_interval
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_komga_series_id"
down_revision: str | None = "0006_publication_poll_interval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(sa.Column("komga_series_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("komga_series_id")
