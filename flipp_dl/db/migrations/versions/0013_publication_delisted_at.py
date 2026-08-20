"""Add delisted_at to publications (TASK-1426).

Revision ID: 0013_publication_delisted_at
Revises: 0012_publication_folder_name
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_publication_delisted_at"
down_revision: str | None = "0012_publication_folder_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(sa.Column("delisted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("delisted_at")
