"""Alembic environment (task 1.2).

Three decisions are worth stating, because each one is a place where the obvious default is wrong
for this project.

**Batch mode is on.** SQLite cannot ``ALTER TABLE`` to add, drop or change a constraint. Alembic's
batch mode emulates it by creating a new table, copying the rows and swapping the names. Phase 1
migrations only create tables, so batch mode changes nothing today — but it has to be configured
before the first migration that needs it, since retrofitting it means rewriting history that has
already been applied somewhere.

**The engine comes from :func:`c360.data.engine.create_sqlite_engine`, not from
``sqlalchemy.url``.** Migrations therefore run with ``foreign_keys = ON`` and establish WAL on the
file, so the schema is created under the same rules the application reads it under. A migration that
inserts a row violating a constraint it just declared fails here rather than at first read. It also
keeps one source of truth for the database path: ``SQLITE_DB_PATH`` via :class:`Settings`.

**Target metadata is ``None``.** There is no ORM model layer to autogenerate against; see
:mod:`c360.data.ddl` for why the DDL is literal.

Path resolution order, most specific first:

1. ``config.attributes["connection"]`` — an open connection supplied by the programmatic runner in
   :mod:`c360.data.migrations`, so a caller already inside a transaction stays inside it;
2. ``config.attributes["db_path"]``;
3. ``-x db_path=...`` on the command line;
4. ``C360_DB_PATH`` in the environment;
5. ``Settings.customer_db_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import Connection

from c360.core.config import get_settings
from c360.data.engine import AccessMode, create_sqlite_engine

config = context.config


def _target_path() -> Path:
    """Resolve the database file to migrate."""
    from_attributes = config.attributes.get("db_path")
    if from_attributes is not None:
        return Path(from_attributes)

    from_argument = context.get_x_argument(as_dictionary=True).get("db_path")
    if from_argument:
        return Path(from_argument)

    from_environment = os.environ.get("C360_DB_PATH")
    if from_environment:
        return Path(from_environment)

    return get_settings().customer_db_path


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=None,
        render_as_batch=True,
        # Recorded in `alembic_version` as-is. Kept short: the table has a length-bounded column
        # and long slugs have historically overflowed it on older Alembic versions.
        transaction_per_migration=True,
        compare_type=False,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of applying it (``alembic upgrade head --sql``).

    Useful for review and for handing a change to a DBA. The URL is nominal — nothing connects.
    """
    context.configure(
        url=f"sqlite:///{_target_path().as_posix()}",
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a real connection."""
    supplied: Connection | None = config.attributes.get("connection")
    if supplied is not None:
        _configure(supplied)
        with context.begin_transaction():
            context.run_migrations()
        return

    # `rwc` because migrating a database that does not exist yet is the normal first run; §12.3's
    # read-only rule governs the *application*, not the tool that builds the file.
    engine = create_sqlite_engine(
        _target_path(),
        mode=AccessMode.READ_WRITE_CREATE,
        pool_size=1,
    )
    try:
        with engine.connect() as connection:
            _configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
