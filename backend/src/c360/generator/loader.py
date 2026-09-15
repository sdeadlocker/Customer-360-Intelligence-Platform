"""Batched load of a generated dataset into SQLite (task 2.8).

Design §15: "Loading uses batched ``executemany`` inside a single transaction with
``PRAGMA synchronous = OFF`` for the load only."

Why each part of that sentence matters
--------------------------------------

**One transaction.** SQLite's cost per transaction is dominated by the commit, not by the row.
Committing
per table would be 26 commits; per row it would be ~25,000, and at the default ``synchronous``
setting
each one waits on the filesystem. The whole load is therefore one transaction, which also makes it
atomic: a constraint violation on the last table leaves no partial database behind.

**``executemany``.** One prepared statement per table, bound repeatedly, rather than one statement
per
row. The statement text comes from :attr:`c360.generator.tables.Table.insert_sql`, built from
module-level literals, so nothing external reaches the SQL.

**``synchronous = OFF`` for the load only.** This is the pragma that removes the fsync per commit.
It is
safe *here* specifically because the database is disposable: if the load is interrupted the file is
discarded and regenerated, so there is no durability requirement to trade away. It is restored to
``NORMAL`` before the connection closes, so nothing inherits the relaxed setting — a detail worth
being
careful about, because :mod:`c360.data.engine` applies ``synchronous = NORMAL`` at connect time and
a
later writer on a pooled connection would otherwise silently run with ``OFF``.

Two things done after the load that the task does not mention
-------------------------------------------------------------

``PRAGMA foreign_key_check`` is run before the commit. Foreign keys are enforced per row anyway —
:mod:`c360.data.engine` sets ``foreign_keys = ON`` on every connection — but the explicit check also
catches a violation against a table that did not exist when the row was inserted, which is exactly
the
``loan.collateral_asset_id`` case migration ``0002`` warns about.

``PRAGMA wal_checkpoint(TRUNCATE)`` is run at the end. The database is opened read-write, so it is
in WAL
mode, and without a checkpoint an arbitrary amount of the data sits in the ``-wal`` sidecar rather
than in
the main file. Task 2.1 requires two runs at the same seed to produce byte-identical databases, and
that
assertion is made against the main file — so where the bytes live is not an implementation detail,
it is
the difference between the test being meaningful and being accidentally true.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from c360.core.logging import get_logger
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.generator.tables import LOAD_ORDER, Dataset

_logger = get_logger(__name__)

#: Rows per ``executemany`` batch. ``executemany`` itself is happy with the whole list, but chunking
#: bounds the size of the parameter sequence SQLite materializes at once, which keeps peak memory
#: flat
#: as the dataset scales to 1,000+ customers.
BATCH_SIZE: Final = 5_000


class LoadError(RuntimeError):
    """Raised when the load fails a referential-integrity check.

    Carries the offending table so the failure names the generator at fault rather than surfacing as
    a
    bare ``IntegrityError`` from the driver.
    """


@dataclass(frozen=True, slots=True)
class LoadReport:
    """What the load did. Returned for the CLI summary and asserted on by the phase-gate test."""

    database: Path
    row_counts: dict[str, int]
    elapsed_seconds: float

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


def _batches(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[start : start + size] for start in range(0, len(rows), size)]


def load(dataset: Dataset, database: Path) -> LoadReport:
    """Insert every staged row into ``database``, in :data:`LOAD_ORDER`.

    The database must already be migrated; :func:`c360.generator.pipeline.seed` handles that.
    Loading
    into an unmigrated file fails on the first insert with ``no such table``, which is the correct
    outcome — silently creating the schema here would give the seeder a second, divergent definition
    of
    it.

    Args:
        dataset: Staged rows.
        database: Target file. Created if absent.

    Returns:
        A :class:`LoadReport` with per-table row counts and the elapsed time.

    Raises:
        LoadError: A foreign-key violation survived the insert.
    """
    started = time.perf_counter()
    engine = create_sqlite_engine(database, mode=AccessMode.READ_WRITE_CREATE, pool_size=1)
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        # Before any DML: the pragma cannot take effect inside an open transaction.
        cursor.execute("PRAGMA synchronous = OFF")

        # The driver opens a deferred transaction before the first INSERT and holds it until
        # commit(),
        # so every table below lands in one transaction without an explicit BEGIN — which the driver
        # would reject as a nested transaction.
        for table in LOAD_ORDER:
            rows = dataset.rows[table.name]
            if not rows:
                continue
            statement = table.insert_sql
            for batch in _batches(rows, BATCH_SIZE):
                cursor.executemany(statement, batch)

        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            connection.rollback()
            tables = sorted({str(row[0]) for row in violations})
            raise LoadError(
                f"{len(violations)} foreign-key violation(s) after load, in: {', '.join(tables)}. "
                "Check the generator's load order in c360.generator.tables.LOAD_ORDER."
            )

        connection.commit()

        # Restore durability before the connection goes back to the pool, then fold the WAL into the
        # main database file so the on-disk bytes are complete. See the module docstring.
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Refresh planner statistics for the freshly-populated tables and indexes (task 16.1). The
        # pipeline runs `recompute` after this, which ANALYZEs again over the derived/graph tables,
        # but doing it here means a database produced by the load path alone still carries the
        # statistics the composite indexes need to be chosen.
        cursor.execute("ANALYZE")
        cursor.close()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        engine.dispose()

    elapsed = time.perf_counter() - started
    counts = dataset.counts()
    _logger.info(
        "dataset loaded",
        extra={
            "database": str(database),
            "tables": len(counts),
            "rows": sum(counts.values()),
            "elapsed_seconds": round(elapsed, 3),
        },
    )
    return LoadReport(database=database, row_counts=counts, elapsed_seconds=elapsed)
