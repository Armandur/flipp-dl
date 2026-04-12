"""Engine and session-factory helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
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
    return engine


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
