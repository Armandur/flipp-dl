"""Add local cover-cache columns to publications and issues.

Covers were hotlinked straight from pagesuite/Flipp on every page render
(TASK-1345). The web UI now serves a locally cached copy instead, so the
DB needs somewhere to remember which file that is:

- ``publications.cover_cache_path`` / ``cover_cache_source_url`` - the
  cached filename and the ``cover_url`` it was fetched from, so a poll
  tick only re-downloads when Flipp actually published a new cover.
- ``issues.cover_cache_path`` - the cached filename for that issue's
  thumbnail, fetched once when the issue is first discovered.

Revision ID: 0005_cover_cache
Revises: 0004_job_indexes
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_cover_cache"
down_revision: str | None = "0004_job_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(
            sa.Column("cover_cache_path", sa.String(length=300), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cover_cache_source_url", sa.String(length=500), nullable=True)
        )
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(
            sa.Column("cover_cache_path", sa.String(length=300), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("cover_cache_path")
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("cover_cache_source_url")
        batch_op.drop_column("cover_cache_path")
