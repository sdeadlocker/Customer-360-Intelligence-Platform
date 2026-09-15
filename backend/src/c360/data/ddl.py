"""Helpers for running hand-written DDL from Alembic migrations.

Migrations in this project carry literal SQL rather than `op.create_table` calls, because design
§4.3 specifies the schema as DDL and the `CHECK` constraints in it are the executable form of
requirement 14.5. Transcribing those into SQLAlchemy `CheckConstraint` objects would add a
translation step between the design and the database with nothing to gain from it.

That leaves two small chores which every migration would otherwise repeat, so they live here:
running a sequence of statements, and dropping a set of tables in an order that respects foreign
keys.

Lives under ``src/`` rather than beside the migration scripts because Alembic loads version files
by path, not by import, so a module next to them is not importable from them. ``prepend_sys_path``
in ``alembic.ini`` puts ``src`` on the path, which makes this the one shared location that works
for both the CLI and the programmatic runner.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

#: Type alias for the statement tuples each migration declares at module level. Declaring the DDL
#: as data rather than as a sequence of calls inside ``upgrade()`` means a test can assert against
#: it directly, and a reviewer can diff it against design §4.3 without reading control flow.
Statements = Sequence[str]


def execute_all(statements: Statements) -> None:
    """Execute each statement in order.

    One statement per call is not an accident: SQLite's driver rejects multi-statement strings on
    ``execute``, and issuing them individually means a failure names the statement that failed
    rather than the whole block.
    """
    for statement in statements:
        op.execute(statement)


def drop_tables(names: Statements) -> None:
    """Drop tables in the given order.

    Callers pass child-before-parent order. With ``foreign_keys = ON`` — which
    :mod:`c360.data.engine` sets on every connection, migrations included — dropping a parent while
    a child still references it fails, so the order is load-bearing rather than cosmetic.
    """
    for name in names:
        op.execute(f"DROP TABLE IF EXISTS {name}")
