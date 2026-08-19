"""Add per-publication poll interval override to publications.

Every watched publication used to be checked for new issues on the same
global poll tick (TASK-1291). Some publications are released monthly and
don't need the same cadence as a weekly one, so this adds an optional
per-publication override:

- ``publications.poll_interval_minutes`` - minutes between checks for this
  publication specifically. ``NULL`` means "use the global default", which
  is today's behaviour: queued/backfilled on every poll tick.
- ``publications.next_poll_due_at`` - bookkeeping for the override above,
  separate from ``last_polled_at`` (which is stamped on every publication
  on every tick regardless of any override).

Revision ID: 0006_publication_poll_interval
Revises: 0005_cover_cache
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_publication_poll_interval"
down_revision: str | None = "0005_cover_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(
            sa.Column("poll_interval_minutes", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("next_poll_due_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("next_poll_due_at")
        batch_op.drop_column("poll_interval_minutes")
