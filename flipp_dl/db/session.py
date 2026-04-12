"""Engine and session-factory helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(db_path: Path | str = ":memory:") -> Engine:
    """Create a SQLite engine.

    Enables WAL mode and foreign-key enforcement so the database is safe
    for concurrent readers + one writer (typical for a small service).
    """
    url = (
        "sqlite:///:memory:"
        if str(db_path) == ":memory:"
        else f"sqlite:///{Path(db_path).resolve()}"
    )
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _on_connect(conn, _record):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    _run_lightweight_migrations(engine)
    return engine


def _run_lightweight_migrations(engine: Engine) -> None:
    """Add columns that were introduced after a DB may have been created.

    SQLAlchemy's ``create_all`` only creates missing tables; it does not
    add new columns to existing ones. For this app we can get away with
    a tiny hand-rolled migration because we only ever add nullable
    columns. For anything more involved, switch to Alembic.
    """
    inspector = inspect(engine)
    if "publications" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("publications")}
        with engine.begin() as conn:
            if "cover_url" not in existing:
                conn.execute(
                    text("ALTER TABLE publications ADD COLUMN cover_url VARCHAR(500)")
                )
            # short_code was briefly introduced in a previous revision and
            # is no longer used; leave any existing column alone (SQLite
            # does not support DROP COLUMN cleanly without a rebuild).


def make_session_factory(db_path: Path | str = ":memory:") -> sessionmaker[Session]:
    """Return a configured :class:`sessionmaker` for *db_path*."""
    engine = make_engine(db_path)
    return sessionmaker(engine)


@contextmanager
def get_session(
    factory: sessionmaker[Session],
) -> Generator[Session, None, None]:
    """Context manager that commits on success and rolls back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
