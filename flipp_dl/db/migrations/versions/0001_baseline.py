"""Baseline schema.

This migration represents the full schema as it was when Alembic was
introduced. On a fresh database it creates every table. On a pre-Alembic
database that already has the tables, ``_ensure_schema`` stamps it as
the current revision instead of running it – see
``flipp_dl/db/session.py``.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("custom_code", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("cover_url", sa.String(500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("next_issue_date", sa.String(20), nullable=True),
        sa.Column("watched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_polled_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "publication_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "publication_id",
            sa.Integer(),
            sa.ForeignKey("publications.id"),
            nullable=False,
        ),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("category_name", sa.String(100), nullable=False),
        sa.UniqueConstraint(
            "publication_id",
            "category_id",
            name="uq_publication_categories_pub_cat",
        ),
    )

    op.create_table(
        "issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "publication_id",
            sa.Integer(),
            sa.ForeignKey("publications.id"),
            nullable=False,
        ),
        sa.Column("custom_code", sa.String(100), nullable=False),
        sa.Column("issue_name", sa.String(255), nullable=False),
        sa.Column("issue_date", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("discovered_at", sa.DateTime(), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("publication_id", "custom_code", name="uq_issues_pub_code"),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("settings")
    op.drop_table("issues")
    op.drop_table("publication_categories")
    op.drop_table("publications")
