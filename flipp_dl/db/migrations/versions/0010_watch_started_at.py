"""Add watch_started_at to publications.

TASK-1361: Watch used to queue the whole back catalogue, and poll's
catch-up pass backfilled anything left NEW/ERROR on every tick regardless
of when it was discovered - fine for a handful of issues, ruinous for a
publication with a large back catalogue (16568 undownloaded issues, ~53
GB average per issue, on the production instance this shipped against).

Watching now only bevakar framåt: it queues nothing itself, and poll's
catch-up pass only fills in issues discovered on/after this timestamp,
never the pre-existing backlog. Fetching the backlog is now the explicit
"Queue missing issues" button's job.

Data migration: every publication that is *already* watched at deploy
time gets ``watch_started_at`` stamped to "now" (migration run time)
rather than left NULL. Left NULL, the next poll's catch-up pass would
have nothing to bound itself against and would either backfill the full
existing backlog (repeating the exact bug this task fixes) or, depending
on how NULL is read, silently stop catching up new issues altogether -
both are exactly the "byt beteende i tysthet" this task explicitly rules
out. Stamping "now" instead means: nothing already sitting in the
database as NEW/ERROR at deploy time is auto-queued, but every issue
discovered by a poll from deploy time onward keeps being picked up
exactly as before - forward watching for the 19 publications already
watched in production is preserved, only the accidental backlog pull is
gone.

Revision ID: 0010_watch_started_at
Revises: 0009_issue_file_size
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0010_watch_started_at"
down_revision: str | None = "0009_issue_file_size"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.add_column(sa.Column("watch_started_at", sa.DateTime(), nullable=True))

    publications = sa.table(
        "publications",
        sa.column("watched", sa.Boolean()),
        sa.column("watch_started_at", sa.DateTime()),
    )
    op.execute(
        publications.update()
        .where(publications.c.watched.is_(True))
        .values(watch_started_at=datetime.now(timezone.utc).replace(tzinfo=None))
    )


def downgrade() -> None:
    with op.batch_alter_table("publications") as batch_op:
        batch_op.drop_column("watch_started_at")
