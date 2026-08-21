"""Add publication_code to publications (TASK-1441).

Revision ID: 0015_publication_code
Revises: 0014_issue_delisted_at
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_publication_code"
down_revision: str | None = "0014_issue_delisted_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(
            sa.Column("publication_code", sa.String(length=20), nullable=True)
        )
        batch_op.create_index("ix_publications_publication_code", ["publication_code"])


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_index("ix_publications_publication_code")
        batch_op.drop_column("publication_code")
