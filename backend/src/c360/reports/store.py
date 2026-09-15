"""The writable ``reports.db`` store, its schema and its own migration head (task 18.1).

``reports.db`` is a writable database in an otherwise read-only platform, created and migrated the
same way as ``audit.db``, ``checkpoints.db`` and ``signals.db``: a self-contained schema applied by
an initializer, carried at its own version so readiness can tell a stale file from a current one. It
has **no foreign key into** ``customer.db`` — customers and books are referenced by id only (task
18.1) — because the two databases are separate files and a cross-file FK is neither expressible nor
desirable here.

Single-writer, WAL
------------------

Writes happen through :class:`ReportStore`, which opens the file ``READ_WRITE_CREATE``, sets WAL +
``synchronous=NORMAL`` and does each unit of work in a single transaction — the same
brief-writer-window shape :mod:`c360.signals.store` uses. The API opens the same file ``mode=ro``
(:mod:`c360.reports.repository`), so readers never contend for a write lock.

What is *not* stored here
-------------------------

Generated artifacts (the PDF bytes, the digest text) live on the ``data/`` volume, not in the
database — the ``report_run`` row carries only the artifact's path and value-free provenance (model
ids, prompt versions, a degraded flag), never customer data or monetary values (task 18.1,
requirement 12.4).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from c360.reports.models import (
    Branding,
    ReportDefinition,
    ReportSchedule,
    ReportScope,
    ReportType,
    RunProvenance,
    RunStatus,
)

#: The schema version this code writes and readiness compares against. Bumped when the DDL below
#: changes, exactly as an Alembic head would be — ``reports.db`` carries its own head, independent
#: of the customer-database migration chain (task 18.1).
SCHEMA_VERSION: Final = "0001_reports"

#: The version bookkeeping table, mirroring the intent of ``alembic_version`` for this store.
_VERSION_TABLE: Final = "reports_schema_version"

_SCHEMA: Final = f"""
CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} (
  version TEXT PRIMARY KEY
);

-- One report definition: what to produce, over what scope, with what branding, owned by whom. No FK
-- into customer.db — scope references customers/segments by bare id (task 18.1). `scope` and
-- `branding` are small JSON structures so they round-trip without a bespoke encoder.
CREATE TABLE IF NOT EXISTS report_definition (
  definition_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  report_type    TEXT NOT NULL CHECK (report_type IN ('PDF_PACK','MEETING_BRIEFING')),
  title          TEXT NOT NULL,
  scope          TEXT NOT NULL CHECK (json_valid(scope)),
  branding       TEXT NOT NULL CHECK (json_valid(branding)),
  owner_id       TEXT NOT NULL,
  owner_role     TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_report_definition_owner ON report_definition(owner_id);

-- A cron-like cadence attached to a definition. `owner_id`/`owner_role` are the entitlement
-- snapshot at creation; the scheduler re-checks the owner's *live* entitlement at run time and
-- never trusts this snapshot (task 18.4).
CREATE TABLE IF NOT EXISTS report_schedule (
  schedule_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  definition_id  INTEGER NOT NULL REFERENCES report_definition(definition_id) ON DELETE CASCADE,
  cadence        TEXT NOT NULL CHECK (cadence IN ('DAILY','WEEKLY','MONTHLY')),
  owner_id       TEXT NOT NULL,
  owner_role     TEXT NOT NULL,
  active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  last_run_at    TEXT,
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_report_schedule_active ON report_schedule(active, definition_id);

-- One execution of a definition. `artifact_path` points at the file on the data/ volume; the bytes
-- are never stored here. `provenance` carries value-free model/prompt labels and the degraded flag
-- (task 18.1, 18.2). No monetary value or customer detail is a column.
CREATE TABLE IF NOT EXISTS report_run (
  run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
  definition_id      INTEGER NOT NULL REFERENCES report_definition(definition_id) ON DELETE CASCADE,
  report_type        TEXT NOT NULL CHECK (report_type IN ('PDF_PACK','MEETING_BRIEFING')),
  status             TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
  triggered_by       TEXT NOT NULL,
  trigger_kind       TEXT NOT NULL CHECK (trigger_kind IN ('MANUAL','SCHEDULED')),
  artifact_path      TEXT,
  customers_rendered INTEGER NOT NULL DEFAULT 0,
  provenance         TEXT NOT NULL CHECK (json_valid(provenance)),
  started_at         TEXT NOT NULL,
  finished_at        TEXT,
  error              TEXT
);

CREATE INDEX IF NOT EXISTS ix_report_run_definition ON report_run(definition_id, run_id DESC);
"""


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The counts a scheduled batch records, returned for the CLI and asserted on by tests."""

    schedules_due: int
    runs_started: int
    runs_succeeded: int
    runs_failed: int

    def summary_lines(self) -> list[str]:
        return [
            f"schedules_due {self.schedules_due:>9,}",
            f"started       {self.runs_started:>9,}",
            f"succeeded     {self.runs_succeeded:>9,}",
            f"failed        {self.runs_failed:>9,}",
        ]


def _now_iso() -> str:
    """UTC timestamp to the second — enough to order runs and schedule state changes."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def initialize_reports_db(path: Path) -> None:
    """Create ``reports.db`` and stamp its schema version if it does not already exist.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` and an ``INSERT OR IGNORE`` of the version, so
    running it against an existing current file is a no-op. Establishes WAL on the file so readers
    inherit it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA)
        connection.execute(
            f"INSERT OR IGNORE INTO {_VERSION_TABLE} (version) VALUES (?)",  # noqa: S608 - constant
            (SCHEMA_VERSION,),
        )
        connection.commit()
    finally:
        connection.close()


def current_schema_version(path: Path) -> str | None:
    """Return the schema version ``reports.db`` is stamped at, or ``None`` if absent/unstamped.

    Opened read-only so a readiness probe (requirement 18.14) can call it without taking a write
    lock, mirroring :func:`c360.signals.store.current_schema_version`.
    """
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_VERSION_TABLE,),
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            f"SELECT version FROM {_VERSION_TABLE} LIMIT 1"  # noqa: S608 - constant name
        ).fetchone()
        return str(row[0]) if row is not None else None
    finally:
        connection.close()


def is_up_to_date(path: Path) -> bool:
    """Whether ``reports.db`` is at the head schema version (requirement 18.14)."""
    return current_schema_version(path) == SCHEMA_VERSION


class ReportStore:
    """The single-writer path into ``reports.db`` (task 18.1, 18.5).

    Not a queue like the audit writer: report writes are occasional (a definition, a schedule, a run
    lifecycle), never on a hot request path, so a plain short-lived write transaction is the right
    shape. Each public method opens a connection, does its work in one transaction and closes it, so
    the store holds no long-lived handle to contend with readers.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    # ---------------------------------------------------------------- definitions
    def create_definition(
        self,
        *,
        report_type: ReportType,
        title: str,
        scope: ReportScope,
        branding: Branding,
        owner_id: str,
        owner_role: str,
    ) -> int:
        """Insert a report definition and return its id (task 18.5)."""
        initialize_reports_db(self._path)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO report_definition (
                      report_type, title, scope, branding, owner_id, owner_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(report_type),
                        title,
                        json.dumps(scope.as_dict(), sort_keys=True),
                        json.dumps(branding.as_dict(), sort_keys=True),
                        owner_id,
                        owner_role,
                        _now_iso(),
                    ),
                )
                return int(cursor.lastrowid or 0)
        finally:
            connection.close()

    def get_definition(self, definition_id: int) -> ReportDefinition | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM report_definition WHERE definition_id = ?", (definition_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_definition(row) if row is not None else None

    # ---------------------------------------------------------------- schedules
    def create_schedule(
        self,
        *,
        definition_id: int,
        cadence: str,
        owner_id: str,
        owner_role: str,
    ) -> int:
        """Attach a cadence to a definition and return the schedule id (task 18.4, 18.5)."""
        initialize_reports_db(self._path)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO report_schedule (
                      definition_id, cadence, owner_id, owner_role, active, created_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (definition_id, cadence, owner_id, owner_role, _now_iso()),
                )
                return int(cursor.lastrowid or 0)
        finally:
            connection.close()

    def set_schedule_active(self, schedule_id: int, *, active: bool) -> bool:
        """Enable or disable a schedule. Returns ``False`` if no such schedule."""
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE report_schedule SET active = ? WHERE schedule_id = ?",
                    (1 if active else 0, schedule_id),
                )
                return cursor.rowcount > 0
        finally:
            connection.close()

    def list_active_schedules(self) -> tuple[ReportSchedule, ...]:
        """Every active schedule, for the scheduler's due-check (task 18.4)."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM report_schedule WHERE active = 1 ORDER BY schedule_id"
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_schedule(row) for row in rows)

    def mark_schedule_ran(self, schedule_id: int, *, ran_at: str) -> None:
        """Record when a schedule last ran, so the next due-check can honour its cadence."""
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE report_schedule SET last_run_at = ? WHERE schedule_id = ?",
                    (ran_at, schedule_id),
                )
        finally:
            connection.close()

    # ---------------------------------------------------------------- runs
    def begin_run(
        self,
        *,
        definition_id: int,
        report_type: ReportType,
        triggered_by: str,
        trigger_kind: str,
    ) -> int:
        """Open a ``report_run`` row in RUNNING and return its id (task 18.5)."""
        initialize_reports_db(self._path)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO report_run (
                      definition_id, report_type, status, triggered_by, trigger_kind,
                      provenance, started_at
                    ) VALUES (?, ?, 'RUNNING', ?, ?, ?, ?)
                    """,
                    (
                        definition_id,
                        str(report_type),
                        triggered_by,
                        trigger_kind,
                        json.dumps(RunProvenance().as_dict(), sort_keys=True),
                        _now_iso(),
                    ),
                )
                return int(cursor.lastrowid or 0)
        finally:
            connection.close()

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        artifact_path: str | None,
        customers_rendered: int,
        provenance: RunProvenance,
        error: str | None = None,
    ) -> None:
        """Close a run with its final status, artifact path and provenance (task 18.5)."""
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE report_run
                       SET status = ?, artifact_path = ?, customers_rendered = ?,
                           provenance = ?, finished_at = ?, error = ?
                     WHERE run_id = ?
                    """,
                    (
                        str(status),
                        artifact_path,
                        customers_rendered,
                        json.dumps(provenance.as_dict(), sort_keys=True),
                        _now_iso(),
                        error,
                        run_id,
                    ),
                )
        finally:
            connection.close()


def now_utc_date() -> str:
    """Today's date in UTC."""
    return datetime.now(UTC).date().isoformat()


def _row_to_definition(row: sqlite3.Row) -> ReportDefinition:
    return ReportDefinition(
        definition_id=int(row["definition_id"]),
        report_type=ReportType(row["report_type"]),
        title=str(row["title"]),
        scope=ReportScope.from_dict(json.loads(row["scope"])),
        branding=Branding.from_dict(json.loads(row["branding"])),
        owner_id=str(row["owner_id"]),
        owner_role=str(row["owner_role"]),
        created_at=str(row["created_at"]),
    )


def _row_to_schedule(row: sqlite3.Row) -> ReportSchedule:
    return ReportSchedule(
        schedule_id=int(row["schedule_id"]),
        definition_id=int(row["definition_id"]),
        cadence=str(row["cadence"]),
        owner_id=str(row["owner_id"]),
        owner_role=str(row["owner_role"]),
        active=bool(row["active"]),
        last_run_at=str(row["last_run_at"]) if row["last_run_at"] is not None else None,
        created_at=str(row["created_at"]),
    )


__all__ = [
    "SCHEMA_VERSION",
    "ReportStore",
    "RunSummary",
    "current_schema_version",
    "initialize_reports_db",
    "is_up_to_date",
    "now_utc_date",
]
