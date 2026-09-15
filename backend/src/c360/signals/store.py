"""The writable ``signals.db`` store, its schema and its own migration head (task 17.1).

``signals.db`` is a writable database in an otherwise read-only platform, created and migrated the
same way as ``audit.db`` and ``checkpoints.db``: a self-contained schema applied by an initializer,
carried at its own version so readiness can tell a stale file from a current one. It has **no
foreign key into** ``customer.db`` — customers are referenced by id only (task 17.1) — because the
two databases are separate files and a cross-file FK is neither expressible nor desirable here.

Single-writer, WAL
------------------

Writes happen in one place: the batch detection job (:mod:`c360.signals.detect`) and the small
state writes for dismiss/ack. Both go through :class:`SignalStore`, which opens the file
``READ_WRITE_CREATE``, sets WAL + ``synchronous=NORMAL`` and does its work in a single transaction —
the same brief-writer-window shape :mod:`c360.data.recompute` uses. The API opens the same file
``mode=ro`` (:mod:`c360.signals.repository`), so readers never contend for a write lock.

Idempotence
-----------

``signal.dedup_key`` is ``UNIQUE``. Re-running detection ``UPSERT``s on that key, so a still-live
concern updates its severity, score, evidence and ``detected_at`` in place rather than accumulating
duplicates (task 17.1, 17.4). ``signal_state`` is keyed ``(signal_id, user_id)`` so each user's
seen/dismissed/actioned state is independent and a dismiss by one RM does not hide a signal from
another entitled to the same book.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from c360.core.logging import get_logger
from c360.core.telemetry import get_meter, scrub_labels
from c360.signals.models import DetectedSignal, SignalStatus

_logger = get_logger(__name__)

#: The schema version this code writes and readiness compares against. Bumped when the DDL below
#: changes, exactly as an Alembic head would be — ``signals.db`` carries its own head, independent
#: of the customer-database migration chain (task 17.1).
SCHEMA_VERSION: Final = "0001_signals"

#: The version bookkeeping table, mirroring the intent of ``alembic_version`` for this store.
_VERSION_TABLE: Final = "signals_schema_version"

_SCHEMA: Final = f"""
CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} (
  version TEXT PRIMARY KEY
);

-- Provenance for one detection batch (task 17.4). Created before `signal` because `signal.run_id`
-- references it. One row per run of the job, so a signal's origin and a run's shape (how many
-- customers scanned, how many signals written) are inspectable.
CREATE TABLE IF NOT EXISTS signal_run (
  run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  as_of            TEXT NOT NULL CHECK (as_of IS date(as_of)),
  customers_scanned INTEGER NOT NULL DEFAULT 0,
  signals_written  INTEGER NOT NULL DEFAULT 0,
  source_db        TEXT NOT NULL
);

-- One derived signal about one customer. No FK into customer.db: customer_id is a bare reference
-- to a row in the separate read-only database (task 17.1). Monetary value is integer cents.
CREATE TABLE IF NOT EXISTS signal (
  signal_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id          TEXT NOT NULL,
  segment              TEXT NOT NULL,
  signal_type          TEXT NOT NULL CHECK (signal_type IN
                         ('RISK_BAND_UP','AML_PEP_FLAG','LARGE_DEPOSIT','LIFE_EVENT')),
  severity             INTEGER NOT NULL CHECK (severity IN (1,2,3)),
  score                REAL NOT NULL CHECK (score >= 0),
  value_at_stake_cents INTEGER NOT NULL DEFAULT 0 CHECK (value_at_stake_cents >= 0),
  evidence             TEXT NOT NULL CHECK (json_valid(evidence)),
  as_of                TEXT NOT NULL CHECK (as_of IS date(as_of)),
  detected_at          TEXT NOT NULL,
  run_id               INTEGER NOT NULL REFERENCES signal_run(run_id),
  dedup_key            TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS ix_signal_customer ON signal(customer_id);
CREATE INDEX IF NOT EXISTS ix_signal_segment ON signal(segment);
CREATE INDEX IF NOT EXISTS ix_signal_rank ON signal(score DESC, signal_id);

-- Per-user state. Keyed (signal_id, user_id) so each caller's triage is independent. A missing row
-- means the signal is NEW for that user. ON DELETE CASCADE so pruning a signal drops its states.
CREATE TABLE IF NOT EXISTS signal_state (
  signal_id  INTEGER NOT NULL REFERENCES signal(signal_id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL,
  status     TEXT NOT NULL CHECK (status IN ('NEW','SEEN','DISMISSED','ACTIONED')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (signal_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_signal_state_user ON signal_state(user_id, status);
"""


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """The counts a detection run records, returned for the CLI and asserted on by tests."""

    run_id: int
    as_of: str
    customers_scanned: int
    signals_written: int

    def summary_lines(self) -> list[str]:
        return [
            f"run           {self.run_id}",
            f"as_of         {self.as_of}",
            f"scanned       {self.customers_scanned:>9,}",
            f"written       {self.signals_written:>9,}",
        ]


def _now_iso() -> str:
    """UTC timestamp to the second — enough to order runs and state changes."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def initialize_signals_db(path: Path) -> None:
    """Create ``signals.db`` and stamp its schema version if it does not already exist.

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
    """Return the schema version ``signals.db`` is stamped at, or ``None`` if absent/unstamped.

    Opened read-only so a readiness probe (requirement 18.14) can call it every few seconds without
    taking a write lock, mirroring :func:`c360.data.migrations.current_revision`.
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
    """Whether ``signals.db`` is at the head schema version (requirement 18.14)."""
    return current_schema_version(path) == SCHEMA_VERSION


class SignalStore:
    """The single-writer path into ``signals.db`` (task 17.1, 17.4).

    Not a queue like the audit writer: signal writes are batch (the detection job) or occasional
    (a dismiss/ack), never on a hot request path, so a plain short-lived write transaction is the
    right shape. Each public method opens a connection, does its work in one transaction and closes
    it, so the store holds no long-lived handle to contend with readers.
    """

    __slots__ = ("_path", "_state_counter")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._state_counter: object = None

    def _record_state_metric(self, status: SignalStatus) -> None:
        """Count a dismiss/ack, labelled by the resulting status (task 17.7).

        The label is the status name (a bounded set), never the signal id, customer or value. The
        counter is created lazily so a disabled telemetry pipeline costs nothing.
        """
        if self._state_counter is None:
            meter = get_meter("c360.signals")
            self._state_counter = meter.create_counter(
                "c360.signals.state_change",
                description="Signal state changes (dismiss/ack), labelled by resulting status.",
            )
        self._state_counter.add(1, scrub_labels({"signal_status": str(status)}))  # type: ignore[attr-defined]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # ---------------------------------------------------------------- detection batch
    def begin_run(self, *, as_of: str, source_db: str) -> int:
        """Open a ``signal_run`` provenance row and return its id (task 17.4)."""
        initialize_signals_db(self._path)
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    "INSERT INTO signal_run (started_at, as_of, source_db) VALUES (?, ?, ?)",
                    (_now_iso(), as_of, source_db),
                )
                run_id = int(cursor.lastrowid or 0)
            return run_id
        finally:
            connection.close()

    def upsert_signals(
        self, run_id: int, signals: Sequence[DetectedSignal], *, scores: Sequence[float]
    ) -> int:
        """Insert or update each signal by ``dedup_key`` (task 17.1, 17.4).

        A still-live concern updates its severity, score, evidence, magnitude, ``as_of`` and
        ``detected_at`` in place, keeping the original ``signal_id`` (and therefore its per-user
        state). ``score`` is precomputed by the ranker so ordering is stored, not recomputed on
        read. Returns the number of rows written.
        """
        if len(signals) != len(scores):
            raise ValueError("signals and scores must be the same length")
        detected_at = _now_iso()
        connection = self._connect()
        try:
            with connection:
                for signal, score in zip(signals, scores, strict=True):
                    connection.execute(
                        """
                        INSERT INTO signal (
                          customer_id, segment, signal_type, severity, score,
                          value_at_stake_cents, evidence, as_of, detected_at, run_id, dedup_key
                        ) VALUES (
                          :customer_id, :segment, :signal_type, :severity, :score,
                          :value_at_stake_cents, :evidence, :as_of, :detected_at, :run_id,
                          :dedup_key
                        )
                        ON CONFLICT(dedup_key) DO UPDATE SET
                          segment = excluded.segment,
                          severity = excluded.severity,
                          score = excluded.score,
                          value_at_stake_cents = excluded.value_at_stake_cents,
                          evidence = excluded.evidence,
                          as_of = excluded.as_of,
                          detected_at = excluded.detected_at,
                          run_id = excluded.run_id
                        """,
                        {
                            "customer_id": signal.customer_id,
                            "segment": signal.evidence.details.get("segment", ""),
                            "signal_type": str(signal.signal_type),
                            "severity": int(signal.severity),
                            "score": float(score),
                            "value_at_stake_cents": int(signal.value_at_stake_cents),
                            "evidence": json.dumps(signal.evidence.as_dict(), sort_keys=True),
                            "as_of": signal.as_of.isoformat(),
                            "detected_at": detected_at,
                            "run_id": run_id,
                            "dedup_key": signal.dedup_key,
                        },
                    )
            return len(signals)
        finally:
            connection.close()

    def finish_run(self, run_id: int, *, customers_scanned: int, signals_written: int) -> None:
        """Close a run's provenance row with its final counts (task 17.4)."""
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE signal_run
                       SET finished_at = ?, customers_scanned = ?, signals_written = ?
                     WHERE run_id = ?
                    """,
                    (_now_iso(), customers_scanned, signals_written, run_id),
                )
        finally:
            connection.close()

    # ---------------------------------------------------------------- per-user state
    def set_state(self, signal_id: int, user_id: str, status: SignalStatus) -> bool:
        """Record a user's state for a signal (dismiss/ack). Returns ``False`` if no such signal.

        Upserts ``signal_state`` so a user can move a signal SEEN → DISMISSED → ACTIONED and back;
        the last write wins. A dismiss is what the ranker's suppression reads (task 17.3).
        """
        connection = self._connect()
        try:
            with connection:
                exists = connection.execute(
                    "SELECT 1 FROM signal WHERE signal_id = ?", (signal_id,)
                ).fetchone()
                if exists is None:
                    return False
                connection.execute(
                    """
                    INSERT INTO signal_state (signal_id, user_id, status, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(signal_id, user_id) DO UPDATE SET
                      status = excluded.status, updated_at = excluded.updated_at
                    """,
                    (signal_id, user_id, str(status), _now_iso()),
                )
            self._record_state_metric(status)
            return True
        finally:
            connection.close()


def now_utc_date() -> str:
    """Today's date in UTC, for a run's ``as_of`` when the source carries none."""
    return datetime.now(UTC).date().isoformat()


__all__ = [
    "SCHEMA_VERSION",
    "RunProvenance",
    "SignalStore",
    "current_schema_version",
    "initialize_signals_db",
    "is_up_to_date",
    "now_utc_date",
]
