"""Add file_size to issues.

Size-estimate feature (TASK-1362): the bulk "Queue missing issues" confirm
dialog needs to say how much a click would download, per publication where
possible and a global fallback otherwise. Computing that from the size of
already-downloaded files means the size has to be readable without
stat()'ing every file on disk on every page render - so it is captured
once, in the database, when an issue is marked done (download or the disk
importer's backfill), and read back from there.

``NULL`` for every issue downloaded before this column existed; the
estimate treats those as "no data" for that issue rather than a zero-byte
file.

Revision ID: 0009_issue_file_size
Revises: 0008_komga_read_status
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_issue_file_size"
down_revision: str | None = "0008_komga_read_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("file_size", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("file_size")
