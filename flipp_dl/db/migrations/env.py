"""Alembic environment script.

Supports two modes:

1. **Embedded** – called from :func:`flipp_dl.db.session._ensure_schema`,
   which opens its own engine/connection and passes it in via
   ``config.attributes["connection"]``. We reuse that connection so the
   migrations run against the same database the rest of the app uses
   (important for ``sqlite:///:memory:`` in tests).

2. **Standalone** – when invoked via the ``alembic`` CLI (e.g. to
   autogenerate a new revision), we build an engine from
   ``sqlalchemy.url`` in ``alembic.ini``.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from flipp_dl.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            _run_with_connection(connection)
    else:
        _run_with_connection(connectable)


def _run_with_connection(connection) -> None:
    # ``render_as_batch=True`` makes Alembic emit SQLite-safe ALTER TABLE
    # sequences (copy-into-new-table) so migrations that drop or rename
    # columns work on the bundled SQLite backend.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
