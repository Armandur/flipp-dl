"""Add per-publication notification opt-in.

Revision ID: 0017_publication_notify
Revises: 0016_publication_destination
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_publication_notify"
down_revision: str | None = "0016_publication_destination"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(
            sa.Column(
                "notify_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    # New publications start silent, but the ones already being watched
    # were notifying before this column existed - keep them that way so
    # the migration doesn't quietly turn off notifications someone relies
    # on. Watching a publication later does not enable it.
    op.execute("UPDATE publications SET notify_enabled = 1 WHERE watched = 1")


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("notify_enabled")
