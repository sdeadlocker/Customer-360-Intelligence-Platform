"""The audit subsystem (task 4.6, design §7.4).

Every customer read, agent invocation, Q&A question, knowledge retrieval, unmask attempt, denied
access and export writes an immutable record here (requirement 12.6). Three design decisions shape
this module:

* **A separate database file.** ``data/audit.db`` is the one writable file in an otherwise
  read-only platform, so the single-writer constraint applies only to audit and never touches the
  customer database's ``mode=ro`` guarantee (design §7.4, §12.3).

* **Append-only, enforced by the database.** SQLite cannot express an INSERT-only grant, so two
  ``BEFORE UPDATE`` / ``BEFORE DELETE`` triggers ``RAISE(ABORT)`` on any attempt to change or remove
  a record. Immutability is a property of the file, not a convention the application is trusted to
  honour (requirement 12.11).

* **Off the request's latency path, but fail-closed.** Records go onto a bounded in-memory queue
  drained by exactly one writer thread that batches inserts in a single transaction — SQLite's
  single-writer model made a virtue. If the queue is full the caller is told so and the request is
  refused rather than served unaudited (design §7.4): an unauditable request is a failure, not a
  fast path.
"""

from __future__ import annotations

import contextlib
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from opentelemetry.metrics import Observation

from c360.core.logging import get_logger
from c360.core.telemetry import get_meter

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_logger = get_logger(__name__)


class AuditOutcome(StrEnum):
    """Whether the audited action was permitted. Mirrors the ``CHECK`` on ``audit_log.outcome``."""

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


# ---------------------------------------------------------------- schema (design §7.4, verbatim)
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS audit_log (
  audit_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at     TEXT NOT NULL,
  actor_user_id   TEXT NOT NULL,
  actor_role      TEXT NOT NULL,
  action          TEXT NOT NULL,
  customer_id     TEXT,
  fields_accessed TEXT CHECK (fields_accessed IS NULL OR json_valid(fields_accessed)),
  outcome         TEXT NOT NULL CHECK (outcome IN ('ALLOWED','DENIED')),
  source_ip       TEXT,
  correlation_id  TEXT NOT NULL,
  trace_id        TEXT,
  request_path    TEXT,
  agent_name      TEXT,
  model_id        TEXT,
  prompt_field_manifest TEXT CHECK (prompt_field_manifest IS NULL
                                    OR json_valid(prompt_field_manifest)),
  retrieved_doc_ids     TEXT CHECK (retrieved_doc_ids IS NULL OR json_valid(retrieved_doc_ids))
);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

_INSERT: Final = """
INSERT INTO audit_log (
  occurred_at, actor_user_id, actor_role, action, customer_id, fields_accessed, outcome,
  source_ip, correlation_id, trace_id, request_path, agent_name, model_id,
  prompt_field_manifest, retrieved_doc_ids
) VALUES (
  :occurred_at, :actor_user_id, :actor_role, :action, :customer_id, :fields_accessed, :outcome,
  :source_ip, :correlation_id, :trace_id, :request_path, :agent_name, :model_id,
  :prompt_field_manifest, :retrieved_doc_ids
)
"""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One audit event, ready to persist.

    The JSON-bearing fields (``fields_accessed``, ``prompt_field_manifest``, ``retrieved_doc_ids``)
    are already-serialized JSON strings or ``None``, matching the ``json_valid`` CHECK; building
    them as strings here keeps the writer thread's insert a pure parameter bind with no per-record
    serialization on the write path.
    """

    occurred_at: str
    actor_user_id: str
    actor_role: str
    action: str
    outcome: AuditOutcome
    correlation_id: str
    customer_id: str | None = None
    fields_accessed: str | None = None
    source_ip: str | None = None
    trace_id: str | None = None
    request_path: str | None = None
    agent_name: str | None = None
    model_id: str | None = None
    prompt_field_manifest: str | None = None
    retrieved_doc_ids: str | None = None

    def as_params(self) -> dict[str, str | None]:
        return {
            "occurred_at": self.occurred_at,
            "actor_user_id": self.actor_user_id,
            "actor_role": self.actor_role,
            "action": self.action,
            "customer_id": self.customer_id,
            "fields_accessed": self.fields_accessed,
            "outcome": str(self.outcome),
            "source_ip": self.source_ip,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
            "request_path": self.request_path,
            "agent_name": self.agent_name,
            "model_id": self.model_id,
            "prompt_field_manifest": self.prompt_field_manifest,
            "retrieved_doc_ids": self.retrieved_doc_ids,
        }


def initialize_audit_db(path: Path) -> None:
    """Create ``audit.db`` and its append-only triggers if they do not already exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_SCHEMA)
        connection.commit()
    finally:
        connection.close()


@dataclass(slots=True)
class _AuditMetrics:
    """Lazily-created instruments so a disabled telemetry pipeline costs nothing."""

    written: object = field(default=None)
    dropped: object = field(default=None)
    queue_depth: list[int] = field(default_factory=lambda: [0])

    def ensure(self) -> None:
        if self.written is not None:
            return
        meter = get_meter("c360.security.audit")
        self.written = meter.create_counter(
            "c360.audit.records_written", description="Audit records durably written."
        )
        self.dropped = meter.create_counter(
            "c360.audit.fail_closed",
            description="Requests refused because the audit queue was saturated (design §7.4).",
        )
        meter.create_observable_gauge(
            "c360.audit.queue_depth",
            callbacks=[lambda _opts: [_observe(self.queue_depth[0])]],
            description="Current audit write-queue depth.",
        )


def _observe(value: int) -> Observation:
    return Observation(value)


class AuditWriter:
    """Bounded-queue, single-writer-thread audit sink. Satisfies :class:`AuditSink`.

    Constructed once at startup and shared. :meth:`submit` is called on the request thread and only
    enqueues; the dedicated writer thread owns the sole connection to ``audit.db`` and does every
    insert, which is what keeps SQLite's single-writer model from ever contending.
    """

    __slots__ = ("_batch_max", "_db_path", "_metrics", "_queue", "_started", "_stop", "_thread")

    #: A poison value the writer thread recognizes as "drain and stop".
    _SENTINEL: Final = None

    def __init__(self, db_path: Path, *, max_queue: int = 10_000, batch_max: int = 128) -> None:
        self._db_path = db_path
        self._queue: queue.Queue[AuditRecord | None] = queue.Queue(maxsize=max_queue)
        self._batch_max = batch_max
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._metrics = _AuditMetrics()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Initialize the database and launch the writer thread. Idempotent."""
        if self._started:
            return
        initialize_audit_db(self._db_path)
        self._metrics.ensure()
        self._thread = threading.Thread(target=self._run, name="c360-audit-writer", daemon=True)
        self._thread.start()
        self._started = True

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the writer to drain and stop, then join it."""
        if not self._started:
            return
        self._stop.set()
        # The sentinel still lands once the queue drains, so a full queue here is harmless.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(self._SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._started = False

    # ---------------------------------------------------------------- the AuditSink port
    def submit(self, record: AuditRecord) -> bool:
        """Enqueue ``record``. Returns ``False`` (fail-closed) if the queue is saturated.

        Never blocks the request thread: a full queue is reported immediately so the caller can
        refuse the request rather than waiting on the writer.
        """
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            _increment(self._metrics.dropped)
            _logger.warning("audit queue saturated; request will fail closed")
            return False
        self._metrics.queue_depth[0] = self._queue.qsize()
        return True

    def is_alive(self) -> bool:
        """Whether the writer thread is running — the readiness check reads this."""
        return self._started and self._thread is not None and self._thread.is_alive()

    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ---------------------------------------------------------------- writer thread
    def _run(self) -> None:
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            while not (self._stop.is_set() and self._queue.empty()):
                batch = self._drain_batch()
                if batch:
                    self._write_batch(connection, batch)
        finally:
            connection.close()

    def _drain_batch(self) -> list[AuditRecord]:
        """Block for one record, then greedily take up to ``batch_max`` more without blocking."""
        batch: list[AuditRecord] = []
        try:
            first = self._queue.get(timeout=0.5)
        except queue.Empty:
            return batch
        if first is self._SENTINEL:
            return batch
        batch.append(first)
        while len(batch) < self._batch_max:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is self._SENTINEL:
                break
            batch.append(item)
        self._metrics.queue_depth[0] = self._queue.qsize()
        return batch

    def _write_batch(self, connection: sqlite3.Connection, batch: Sequence[AuditRecord]) -> None:
        try:
            with connection:
                connection.executemany(_INSERT, [record.as_params() for record in batch])
            _increment(self._metrics.written, len(batch))
        except sqlite3.Error:
            # A failed audit write is serious, but the writer thread must not die — that would turn
            # a transient error into a permanent fail-closed. Log (redacted) and continue; the queue
            # backpressure and the fail-closed counter surface a sustained problem.
            _logger.exception("audit batch write failed", extra={"batch_size": len(batch)})


def _increment(counter: object, amount: int = 1) -> None:
    """Add to a counter if telemetry produced one."""
    if counter is not None:
        counter.add(amount)  # type: ignore[attr-defined]


def now_iso() -> str:
    """UTC timestamp for ``occurred_at``, to the second — enough to order audit events."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
