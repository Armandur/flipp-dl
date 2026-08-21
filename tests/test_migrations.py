"""Migrations must survive a database that already has rows and children.

Regression for the 0017 failure in production: a migration that makes
Alembic rebuild ``publications`` on SQLite (which it does for a NOT NULL
column) fails on ``DROP TABLE publications`` with "FOREIGN KEY constraint
failed", because ``issues`` references it and every connection runs with
``PRAGMA foreign_keys=ON``. The failed attempt also left
``_alembic_tmp_publications`` behind, so every later start failed too.

Creating a database from scratch does not catch this - the tables are
created at head, no migration ever rebuilds them. The test walks the
migration path over a populated database instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command as alembic_command

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import _alembic_config, get_session, make_session_factory
from flipp_dl.models import Issue, Publication


def _seed(session_factory) -> None:
    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(
            Publication(custom_code="KA", name="Kalle Anka")
        )
        repo.set_watched("KA", True)
        repo.upsert_issue(
            Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01"),
            db_pub.id,
        )


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in con.execute("select name from sqlite_master where type='table'")
        }
    finally:
        con.close()


def test_upgrade_from_the_previous_revision_over_populated_tables(tmp_path):
    db_path = tmp_path / "flipp.db"
    factory = make_session_factory(db_path)
    _seed(factory)

    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.downgrade(cfg, "0016_publication_destination")

    # Upgrade the way the application does, not through a bare Alembic
    # config: make_session_factory's engine sets PRAGMA foreign_keys=ON,
    # and that pragma is exactly what turned the table rebuild into a
    # failure in production.
    make_session_factory(db_path)

    assert "_alembic_tmp_publications" not in _tables(db_path)
    con = sqlite3.connect(db_path)
    try:
        # The publication survived, its issue survived, and a watched
        # publication keeps notifying across the upgrade.
        assert con.execute("select count(*) from publications").fetchone()[0] == 1
        assert con.execute("select count(*) from issues").fetchone()[0] == 1
        assert (
            con.execute(
                "select notify_enabled from publications where custom_code='KA'"
            ).fetchone()[0]
            == 1
        )
    finally:
        con.close()


def test_upgrade_cleans_up_debris_from_an_interrupted_rebuild(tmp_path):
    """A half-finished batch migration must not brick every later start."""
    db_path = tmp_path / "flipp.db"
    factory = make_session_factory(db_path)
    _seed(factory)

    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.downgrade(cfg, "0016_publication_destination")

    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE _alembic_tmp_publications (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()

    alembic_command.upgrade(cfg, "head")

    assert "_alembic_tmp_publications" not in _tables(db_path)
