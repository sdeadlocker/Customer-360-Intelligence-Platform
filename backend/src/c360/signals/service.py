"""The worklist read model and the dismiss/ack write path (task 17.3, 17.5).

Sits between the routes and the store/repository: it owns cursor encoding, the entitlement-scoped
ranked read, the per-customer drill-down and the state writes. Signals themselves are already ranked
and suppressed by the repository query; this layer adds the API-shaped paging and the write
delegation, and nothing else — the deterministic work is all upstream.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from c360.data.engine import AccessMode, DatabaseFileMissingError, create_sqlite_engine
from c360.signals.models import Severity, Signal, SignalStatus, SignalType
from c360.signals.repository import SignalRepository
from c360.signals.store import SignalStore

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from c360.security.entitlement import EntitlementScope


@dataclass(frozen=True, slots=True)
class WorklistPage:
    """One page of the ranked worklist plus the opaque cursor for the next."""

    items: tuple[Signal, ...]
    next_cursor: str | None


def _encode_cursor(score: float, signal_id: int) -> str:
    return base64.urlsafe_b64encode(f"{score}:{signal_id}".encode()).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[float, int] | None:
    """Decode a ``score:id`` keyset cursor, or ``None`` for the first page.

    A malformed cursor decodes to ``None`` (start from the top) rather than raising: a cursor is
    opaque client state, and the safe reading of a garbled one can never widen what the caller sees,
    because entitlement scoping is applied independently in the query (mirrors pagination.py).
    """
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        score_str, id_str = raw.rsplit(":", 1)
        return float(score_str), int(id_str)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


class SignalService:
    """The worklist read model and dismiss/ack writer (task 17.3, 17.5)."""

    __slots__ = ("_cooling_off_days", "_repository", "_store")

    def __init__(
        self,
        repository: SignalRepository,
        store: SignalStore,
        *,
        cooling_off_days: int,
    ) -> None:
        if cooling_off_days < 0:
            raise ValueError("cooling_off_days must be non-negative")
        self._repository = repository
        self._store = store
        self._cooling_off_days = cooling_off_days

    def worklist(
        self,
        scope: EntitlementScope,
        user_id: str,
        *,
        limit: int,
        cursor: str | None = None,
        signal_types: tuple[SignalType, ...] | None = None,
        min_severity: Severity | None = None,
        today: date | None = None,
    ) -> WorklistPage:
        """The caller's ranked, entitlement-scoped, suppression-aware worklist (task 17.5)."""
        keyset = _decode_cursor(cursor)
        page = self._repository.list_ranked(
            scope,
            user_id,
            limit=limit,
            after_score=keyset[0] if keyset else None,
            after_id=keyset[1] if keyset else None,
            signal_types=signal_types,
            min_severity=min_severity,
            cooling_off_days=self._cooling_off_days,
            today=today or datetime.now(UTC).date(),
        )
        next_cursor = None
        if page.next_cursor is not None and page.items:
            last = page.items[-1]
            next_cursor = _encode_cursor(last.score, last.signal_id)
        return WorklistPage(items=page.items, next_cursor=next_cursor)

    def for_customer(self, customer_id: str, user_id: str) -> tuple[Signal, ...]:
        """Every signal for one customer, for the drill-down (task 17.5)."""
        return self._repository.list_for_customer(customer_id, user_id)

    def dismiss(self, signal_id: int, user_id: str) -> bool:
        """Mark a signal dismissed for the caller (task 17.5). ``False`` if no such signal."""
        return self._store.set_state(signal_id, user_id, SignalStatus.DISMISSED)

    def acknowledge(self, signal_id: int, user_id: str) -> bool:
        """Mark a signal actioned for the caller (task 17.5). ``False`` if no such signal."""
        return self._store.set_state(signal_id, user_id, SignalStatus.ACTIONED)

    def customer_of(self, signal_id: int) -> str | None:
        """The customer a signal is about, for the entitlement re-check on dismiss/ack."""
        return self._repository.get_customer_id(signal_id)


def build_signal_service(
    signals_db_path: Path, *, cooling_off_days: int, pool_size: int
) -> tuple[SignalService, Engine] | None:
    """Open ``signals.db`` read-only and construct the service, or ``None`` if not yet built.

    Best-effort, mirroring the knowledge service: a deployment that has not run
    ``c360 detect-signals`` yet still serves the whole platform, and the worklist endpoints report
    an empty feed rather than crashing. The store (writer) uses the same path for dismiss/ack.
    """
    if not signals_db_path.is_file():
        return None
    try:
        engine = create_sqlite_engine(
            signals_db_path, mode=AccessMode.READ_ONLY, pool_size=pool_size
        )
    except DatabaseFileMissingError:
        return None
    repository = SignalRepository(engine)
    store = SignalStore(signals_db_path)
    return SignalService(repository, store, cooling_off_days=cooling_off_days), engine


__all__ = ["SignalService", "WorklistPage", "build_signal_service"]
