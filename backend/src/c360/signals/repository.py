"""Read-only access to ``signals.db`` for the worklist API (task 17.5).

The API opens ``signals.db`` ``mode=ro`` — the same read-only rule the customer and knowledge
databases follow — so a read path can never take a write lock or contend with the detection job.
Writes (dismiss/ack) go through :class:`~c360.signals.store.SignalStore`, never here.

Entitlement scoping is applied **in SQL**, before ``LIMIT``, exactly as customer listing does
(design §4.4): the caller's :class:`~c360.security.entitlement.EntitlementScope` renders a predicate
over ``customer_id`` / ``segment`` and is spliced into the ``WHERE`` clause, so a restricted book
never loses a ranked signal it was entitled to and the queue never reveals a signal outside the
book. Dismissed and cooled-off signals are filtered here too, so suppression is a query property
rather than a post-filter (task 17.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from c360.security.entitlement import customer_predicate
from c360.signals.models import (
    Evidence,
    Severity,
    Signal,
    SignalStatus,
    SignalType,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from c360.security.entitlement import EntitlementScope


@dataclass(frozen=True, slots=True)
class SignalPage:
    """One page of ranked signals plus the keyset cursor for the next (task 17.5)."""

    items: tuple[Signal, ...]
    next_cursor: str | None


def _row_to_signal(row: Any, status: SignalStatus) -> Signal:
    return Signal(
        signal_id=int(row.signal_id),
        customer_id=str(row.customer_id),
        signal_type=SignalType(row.signal_type),
        severity=Severity(int(row.severity)),
        score=float(row.score),
        value_at_stake_cents=int(row.value_at_stake_cents),
        evidence=Evidence.from_dict(json.loads(row.evidence)),
        as_of=date.fromisoformat(row.as_of),
        detected_at=str(row.detected_at),
        dedup_key=str(row.dedup_key),
        status=status,
    )


class SignalRepository:
    """Read-only reads of ``signals.db``, entitlement-scoped and suppression-aware."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_ranked(
        self,
        scope: EntitlementScope,
        user_id: str,
        *,
        limit: int,
        after_score: float | None = None,
        after_id: int | None = None,
        signal_types: tuple[SignalType, ...] | None = None,
        min_severity: Severity | None = None,
        cooling_off_days: int,
        today: date,
    ) -> SignalPage:
        """The caller's ranked worklist across their entitled book (task 17.3, 17.5).

        Ordered by descending score then ascending id (a total, stable key so the keyset cursor is
        unambiguous). Dismissed signals are suppressed while within ``cooling_off_days`` of the
        dismissal; a signal actioned by the caller is always suppressed. Optional ``signal_types``
        and ``min_severity`` filters power the UI's type/severity chips.
        """
        predicate = customer_predicate(scope, id_column="s.customer_id", segment_column="s.segment")
        params: dict[str, object] = dict(predicate.params)
        params["user_id"] = user_id
        params["limit"] = limit
        params["cooling_cutoff"] = today.isoformat()
        params["cooling_days"] = cooling_off_days

        filters = [f"({predicate.sql})"]
        # Suppress signals the caller dismissed within the cooling-off window, and anything they
        # actioned. A NEW/SEEN state, or no state row, stays visible.
        # A missing state row (st.status IS NULL) means the signal is NEW for this user and must
        # stay visible. Comparisons against NULL yield NULL (not false), so ``NOT (...)`` over a
        # NULL status would drop every un-triaged signal — hence the explicit NULL guard.
        filters.append("""
            (
              st.status IS NULL
              OR (
                st.status <> 'ACTIONED'
                AND NOT (
                  st.status = 'DISMISSED'
                  AND date(st.updated_at) > date(:cooling_cutoff, '-' || :cooling_days || ' days')
                )
              )
            )
            """)
        if after_score is not None and after_id is not None:
            params["after_score"] = after_score
            params["after_id"] = after_id
            # Keyset: strictly after (score desc, id asc).
            filters.append(
                "(s.score < :after_score "
                "OR (s.score = :after_score AND s.signal_id > :after_id))"
            )
        if signal_types:
            names = [f"stype_{i}" for i in range(len(signal_types))]
            params.update({name: str(t) for name, t in zip(names, signal_types, strict=True)})
            filters.append("s.signal_type IN (" + ", ".join(f":{n}" for n in names) + ")")
        if min_severity is not None:
            params["min_sev"] = int(min_severity)
            filters.append("s.severity >= :min_sev")

        where = " AND ".join(filters)
        sql = f"""
            SELECT s.signal_id, s.customer_id, s.signal_type, s.severity, s.score,
                   s.value_at_stake_cents, s.evidence, s.as_of, s.detected_at, s.dedup_key,
                   COALESCE(st.status, 'NEW') AS status
            FROM signal s
            LEFT JOIN signal_state st ON st.signal_id = s.signal_id AND st.user_id = :user_id
            WHERE {where}
            ORDER BY s.score DESC, s.signal_id ASC
            LIMIT :limit
        """  # noqa: S608 - all interpolation is parameterized fragments / bound placeholders
        with self._engine.connect() as connection:
            rows = list(connection.execute(text(sql), params).all())
        items = tuple(_row_to_signal(row, SignalStatus(row.status)) for row in rows)
        next_cursor = None
        if len(items) == limit and items:
            last = items[-1]
            next_cursor = f"{last.score}:{last.signal_id}"
        return SignalPage(items=items, next_cursor=next_cursor)

    def list_for_customer(self, customer_id: str, user_id: str) -> tuple[Signal, ...]:
        """Every signal for one customer, ranked, with the caller's state folded in (task 17.5).

        Entitlement for the customer itself is enforced by the route before this is called (the
        same 403-vs-404 gate the 360 view uses), so this does not re-scope — it simply returns the
        customer's signals for a drill-down that has already passed authorization.
        """
        sql = """
            SELECT s.signal_id, s.customer_id, s.signal_type, s.severity, s.score,
                   s.value_at_stake_cents, s.evidence, s.as_of, s.detected_at, s.dedup_key,
                   COALESCE(st.status, 'NEW') AS status
            FROM signal s
            LEFT JOIN signal_state st ON st.signal_id = s.signal_id AND st.user_id = :user_id
            WHERE s.customer_id = :customer_id
            ORDER BY s.score DESC, s.signal_id ASC
        """
        with self._engine.connect() as connection:
            rows = list(
                connection.execute(
                    text(sql), {"customer_id": customer_id, "user_id": user_id}
                ).all()
            )
        return tuple(_row_to_signal(row, SignalStatus(row.status)) for row in rows)

    def get_customer_id(self, signal_id: int) -> str | None:
        """The customer a signal is about, for the entitlement re-check on dismiss/ack."""
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT customer_id FROM signal WHERE signal_id = :sid"),
                {"sid": signal_id},
            ).fetchone()
        return str(row[0]) if row is not None else None


__all__ = ["SignalPage", "SignalRepository"]
