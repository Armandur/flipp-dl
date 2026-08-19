"""Engine and session-factory helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_ALEMBIC_INI = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# How long a writer waits for a competing writer before giving up.
_LOCK_TIMEOUT_SECONDS = 30


def make_engine(db_path: Path | str = ":memory:") -> Engine:
    """Create a SQLite engine and bring the schema up to date.

    Enables WAL mode and foreign-key enforcement so the database is safe
    for concurrent readers + one writer (typical for a small service).
    All schema management is delegated to Alembic – see
    ``flipp_dl/db/migrations/``.
    """
    url = (
        "sqlite:///:memory:"
        if str(db_path) == ":memory:"
        else f"sqlite:///{Path(db_path).resolve()}"
    )
    engine = create_engine(
        url,
        connect_args={
            "check_same_thread": False,
            # Wait for a competing writer instead of raising "database is
            # locked" straight away. The scheduler thread writes download
            # progress while the web thread serves pages, and SQLite's
            # 5-second default was not enough on a busy instance.
            "timeout": _LOCK_TIMEOUT_SECONDS,
        },
    )

    @event.listens_for(engine, "connect")
    def _on_connect(conn, _record):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Same wait, applied to the SQLite side of the connection.
        conn.execute(f"PRAGMA busy_timeout={_LOCK_TIMEOUT_SECONDS * 1000}")

    _ensure_schema(engine)
    return engine


def _alembic_config() -> AlembicConfig:
    """Build an Alembic ``Config`` pointing at our migrations package.

    We avoid relying on the on-disk ``alembic.ini`` at runtime so the
    application keeps working inside wheels/zipapps that don't ship the
    ini file. The ``script_location`` is set explicitly and the URL is
    supplied per-call via the shared connection on ``cfg.attributes``.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    # ``sqlalchemy.url`` has to be set for Alembic to build a Config,
    # but it's ignored because env.py uses the shared connection.
    cfg.set_main_option("sqlalchemy.url", "sqlite:///")
    return cfg


def _ensure_schema(engine: Engine) -> None:
    """Make sure the database is at the latest Alembic revision.

    Handles three cases:

    * Fresh database → ``upgrade head`` runs the baseline migration and
      creates all tables.
    * Existing Alembic-managed database → ``upgrade head`` is a no-op if
      already current, or applies pending migrations otherwise.
    * Pre-Alembic database (tables already exist from the hand-rolled
      schema) → we ``stamp head`` so future migrations can run against
      it without re-creating tables.
    """
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        cfg = _alembic_config()
        cfg.attributes["connection"] = connection

        if "alembic_version" not in tables and "publications" in tables:
            # Pre-Alembic schema – mark it as current without running
            # the baseline (which would fail on "table already exists").
            alembic_command.stamp(cfg, "head")
        else:
            alembic_command.upgrade(cfg, "head")
        connection.commit()


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
