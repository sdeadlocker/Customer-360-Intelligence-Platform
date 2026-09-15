"""SQLite adapter for :class:`~c360.domain.ports.JourneyRepository` (task 1.6).

Each timeline component is read separately rather than merged in SQL. A ``UNION ALL`` across four
tables with different shapes would have to flatten them into a lowest-common-denominator row, and
requirement 7.1's timeline entries are not interchangeable — an application shows a fraud result, a
life event shows cited signals. Merging is :class:`JourneyService`'s job in Phase 5, over typed
models.

What the schema *does* provide is the uniform ``(customer_id, event_date)`` index on all four tables
(design §4.5), so each of these reads is an indexed range scan rather than a scan plus a sort.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from c360.data.repositories.base import (
    SqliteRepository,
    Statement,
    expand_in_clause,
    to_models,
)
from c360.domain.models import Application, EngagementEvent, LifeEvent, Transaction

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.domain.enums import EngagementChannel, EngagementEventType

_LIFE_EVENTS = Statement(
    id="journey.life_events",
    collection="life_event",
    sql="""
    SELECT le.life_event_id, le.customer_id, le.life_event_type, le.event_date, le.confidence,
           le.source, le.is_inferred, le.signals, le.as_of_date, c.source_system
    FROM life_event le
    JOIN customer c ON c.customer_id = le.customer_id
    WHERE le.customer_id = :customer_id
    ORDER BY le.event_date DESC, le.life_event_id
    """,
)

_APPLICATIONS = Statement(
    id="journey.applications",
    collection="application",
    sql="""
    SELECT application_id, customer_id, product_applied, product_code, event_date, channel,
           application_status, fraud_result, decision_date, requested_amount_cents,
           approved_amount_cents, account_id, as_of_date, source_system
    FROM application
    WHERE customer_id = :customer_id
    ORDER BY event_date DESC, application_id
    """,
)


def _engagement_statement(
    *,
    channel_placeholders: str | None,
    type_placeholders: str | None,
) -> Statement:
    """Build the engagement statement with the requirement 7.6 filters applied in SQL."""
    clauses = ["ce.customer_id = :customer_id"]
    if channel_placeholders:
        clauses.append(f"ce.channel IN ({channel_placeholders})")
    if type_placeholders:
        clauses.append(f"ce.event_type IN ({type_placeholders})")
    return Statement(
        id="journey.engagement_events",
        collection="customer_event",
        # Interpolated parts are generated placeholder *names* from `expand_in_clause`, never
        # values;
        # see the note in `c360.data.repositories.financial._holdings_statement`.
        sql=f"""
        SELECT ce.event_id, ce.customer_id, ce.event_type, ce.event_date, ce.channel,
               ce.session_id, ce.device_type, ce.outcome, ce.notes, ce.as_of_date, c.source_system
        FROM customer_event ce
        JOIN customer c ON c.customer_id = ce.customer_id
        WHERE {" AND ".join(clauses)}
        ORDER BY ce.event_date DESC, ce.event_id
        LIMIT :limit
        """,  # noqa: S608
    )


_MAJOR_TRANSACTIONS = Statement(
    id="journey.major_transactions",
    collection="txn",
    # Requirement 7.4: a transaction is major if it clears the absolute threshold **or** the median
    # multiple. Both comparisons are integer:
    #
    #   abs(amount) * 10000 >= median * median_multiple_bps
    #
    # rather than `abs(amount) >= median * multiple` with a float multiple. Scaling both sides by
    # 10,000 keeps the whole predicate in integers, which is design §1.1's rule applied to a
    # comparison rather than to a stored value — a float multiple would make the boundary case
    # depend
    # on binary rounding.
    #
    # Debits only: a large salary credit is not a "major transaction" in the sense requirement 7.1's
    # timeline means, and including credits would put every payday on the timeline.
    sql="""
    WITH median_debit AS (
      SELECT abs(amount_cents) AS amount
      FROM txn
      WHERE customer_id = :customer_id AND amount_cents < 0
      ORDER BY abs(amount_cents)
      LIMIT 1
      OFFSET (SELECT COUNT(*) / 2 FROM txn WHERE customer_id = :customer_id AND amount_cents < 0)
    )
    SELECT t.transaction_id, t.account_id, t.customer_id, t.transaction_date, t.amount_cents,
           t.transaction_type, t.transaction_category, t.merchant, t.channel, t.status,
           t.transaction_date AS as_of_date, c.source_system
    FROM txn t
    JOIN customer c ON c.customer_id = t.customer_id
    WHERE t.customer_id = :customer_id
      AND t.amount_cents < 0
      AND (
        abs(t.amount_cents) >= :absolute_threshold_cents
        OR abs(t.amount_cents) * 10000 >=
           (SELECT amount FROM median_debit) * :median_multiple_bps
      )
    ORDER BY abs(t.amount_cents) DESC, t.transaction_id
    LIMIT :limit
    """,
)


class SqliteJourneyRepository(SqliteRepository):
    """Reads backing requirements 7.1 through 7.6."""

    def list_life_events(self, customer_id: str) -> Sequence[LifeEvent]:
        rows = self.fetch_all(_LIFE_EVENTS, {"customer_id": customer_id})
        return to_models(LifeEvent, rows)

    def list_applications(self, customer_id: str) -> Sequence[Application]:
        rows = self.fetch_all(_APPLICATIONS, {"customer_id": customer_id})
        return to_models(Application, rows)

    def list_engagement_events(
        self,
        customer_id: str,
        *,
        channels: Sequence[EngagementChannel] | None = None,
        event_types: Sequence[EngagementEventType] | None = None,
        limit: int = 200,
    ) -> Sequence[EngagementEvent]:
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        params: dict[str, Any] = {"customer_id": customer_id, "limit": limit}
        channel_placeholders = type_placeholders = None

        if channels is not None:
            if not channels:
                return ()
            channel_placeholders, bindings = expand_in_clause(
                "channel", [str(value) for value in channels]
            )
            params.update(bindings)
        if event_types is not None:
            if not event_types:
                return ()
            type_placeholders, bindings = expand_in_clause(
                "event_type", [str(value) for value in event_types]
            )
            params.update(bindings)

        rows = self.fetch_all(
            _engagement_statement(
                channel_placeholders=channel_placeholders,
                type_placeholders=type_placeholders,
            ),
            params,
        )
        return to_models(EngagementEvent, rows)

    def list_major_transactions(
        self,
        customer_id: str,
        *,
        absolute_threshold_cents: int,
        median_multiple_bps: int,
        limit: int = 50,
    ) -> Sequence[Transaction]:
        if absolute_threshold_cents < 1:
            raise ValueError(
                f"absolute_threshold_cents must be positive, got {absolute_threshold_cents}"
            )
        if median_multiple_bps < 1:
            raise ValueError(f"median_multiple_bps must be positive, got {median_multiple_bps}")
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")

        rows = self.fetch_all(
            _MAJOR_TRANSACTIONS,
            {
                "customer_id": customer_id,
                "absolute_threshold_cents": absolute_threshold_cents,
                "median_multiple_bps": median_multiple_bps,
                "limit": limit,
            },
        )
        return to_models(Transaction, rows)
