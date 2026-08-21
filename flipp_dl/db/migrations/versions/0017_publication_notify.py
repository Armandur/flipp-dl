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
    # An earlier version of this migration declared the column NOT NULL,
    # which makes Alembic rebuild the whole table on SQLite: create a
    # temp copy, DROP TABLE publications, rename. That DROP fails with
    # "FOREIGN KEY constraint failed" because issues and
    # publication_categories reference publications and the connection
    # runs with PRAGMA foreign_keys=ON - and it leaves the temp table
    # behind, so every later start failed on "table
    # _alembic_tmp_publications already exists". Clean that up first for
    # any database that hit it.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_publications")

    # Nullable: SQLite can add a nullable column in place, no rebuild and
    # therefore no foreign keys in the way. NULL means the same as false.
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(sa.Column("notify_enabled", sa.Boolean(), nullable=True))
    op.execute("UPDATE publications SET notify_enabled = 0")
    # New publications start silent, but the ones already being watched
    # were notifying before this column existed - keep them that way so
    # the migration doesn't quietly turn off notifications someone relies
    # on. Watching a publication later does not enable it.
    op.execute("UPDATE publications SET notify_enabled = 1 WHERE watched = 1")


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("notify_enabled")
