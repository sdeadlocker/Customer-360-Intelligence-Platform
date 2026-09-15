"""``JourneyService`` — merged timeline and engagement history (task 5.6, design §6.1).

The repository reads each timeline component separately and typed — life events, applications, major
transactions, engagement events — because they are not interchangeable (requirement 7.1). Merging
them into one chronologically-ordered timeline is this service's job: it maps each component to a
uniform :class:`TimelineEntry` carrying a category, a date, a title and a source reference, then
sorts the union newest-first.

The major-transaction rule (requirement 7.4)
--------------------------------------------

A transaction is "major" if it clears an absolute threshold **or** a multiple of the customer's
median transaction, either criterion. Both are configurable
(``MAJOR_TXN_ABSOLUTE_THRESHOLD_CENTS``, ``MAJOR_TXN_MEDIAN_MULTIPLE``). The repository takes both
as
integers, the multiple in basis points — a float multiple in a money comparison would put binary
rounding on the boundary case (design §1.1) — so the service converts the configured ``10.0`` once
into ``100_000`` bps here and never lets the float reach the predicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.data.repositories.journey import SqliteJourneyRepository
    from c360.data.repositories.relationship import SqliteRelationshipRepository
    from c360.domain.enums import EngagementChannel, EngagementEventType
    from c360.domain.models import EngagementEvent, LifeEvent

#: Basis-point scale: a configured multiple of 10.0x becomes 100_000 bps. Kept as a named constant
#: so
#: the conversion is not a bare magic number at the call site.
_BPS_PER_UNIT = 10_000


class TimelineCategory(StrEnum):
    """The kinds of entry a merged timeline carries (requirement 7.1)."""

    LIFE_EVENT = "LIFE_EVENT"
    APPLICATION = "APPLICATION"
    MAJOR_TRANSACTION = "MAJOR_TRANSACTION"
    RELATIONSHIP_CHANGE = "RELATIONSHIP_CHANGE"


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One entry on the merged customer timeline (requirement 7.1).

    ``category`` says which component it came from, ``entry_date`` is the date it is placed at,
    ``title`` is a short value-free label, and ``source_id`` references the underlying row so a UI
    can
    drill into it. ``amount_cents`` is populated only for the entries that have a monetary value
    (a major transaction, a funded application), ``None`` otherwise.
    """

    category: TimelineCategory
    entry_date: date
    title: str
    source_id: str
    amount_cents: int | None = None


class JourneyService:
    """Timeline assembly and engagement history over the journey and relationship repositories."""

    __slots__ = (
        "_absolute_threshold_cents",
        "_journey",
        "_major_txn_limit",
        "_median_multiple_bps",
        "_relationship",
    )

    def __init__(
        self,
        journey: SqliteJourneyRepository,
        relationship: SqliteRelationshipRepository,
        *,
        major_txn_absolute_threshold_cents: int,
        major_txn_median_multiple: float,
        major_txn_limit: int = 50,
    ) -> None:
        if major_txn_absolute_threshold_cents < 1:
            raise ValueError("major_txn_absolute_threshold_cents must be positive")
        if major_txn_median_multiple <= 0:
            raise ValueError("major_txn_median_multiple must be positive")
        self._journey = journey
        self._relationship = relationship
        self._absolute_threshold_cents = major_txn_absolute_threshold_cents
        # Convert the configured float multiple to integer basis points once, here, so the money
        # comparison in SQL stays integer (design §1.1).
        self._median_multiple_bps = max(1, round(major_txn_median_multiple * _BPS_PER_UNIT))
        self._major_txn_limit = major_txn_limit

    def get_timeline(self, customer_id: str) -> tuple[TimelineEntry, ...]:
        """The merged, newest-first timeline across every component (requirement 7.1-7.5)."""
        entries: list[TimelineEntry] = []
        entries.extend(self._life_event_entries(customer_id))
        entries.extend(self._application_entries(customer_id))
        entries.extend(self._major_transaction_entries(customer_id))
        entries.extend(self._relationship_entries(customer_id))
        # Newest first; a stable secondary key on source_id keeps same-day entries reproducible.
        entries.sort(key=lambda entry: (entry.entry_date, entry.source_id), reverse=True)
        return tuple(entries)

    def get_life_events(self, customer_id: str) -> Sequence[LifeEvent]:
        """The customer's life events, recorded and inferred (requirements 7.2, 7.3).

        The timeline folds these into :class:`TimelineEntry` rows, but a caller that needs the
        events themselves — the tool layer, an agent reasoning over inferred events and their
        signals — needs the typed :class:`~c360.domain.models.LifeEvent`, so it is exposed directly
        here rather than only through the merged timeline.
        """
        return self._journey.list_life_events(customer_id)

    def get_engagement_history(
        self,
        customer_id: str,
        *,
        channels: Sequence[EngagementChannel] | None = None,
        event_types: Sequence[EngagementEventType] | None = None,
        limit: int = 200,
    ) -> Sequence[EngagementEvent]:
        """Engagement events, newest first, filterable by channel and type (requirement 7.6)."""
        return self._journey.list_engagement_events(
            customer_id, channels=channels, event_types=event_types, limit=limit
        )

    # ---------------------------------------------------------------- component mappers
    def _life_event_entries(self, customer_id: str) -> list[TimelineEntry]:
        return [
            TimelineEntry(
                category=TimelineCategory.LIFE_EVENT,
                entry_date=event.event_date,
                title=event.life_event_type.value,
                source_id=event.life_event_id,
            )
            for event in self._journey.list_life_events(customer_id)
        ]

    def _application_entries(self, customer_id: str) -> list[TimelineEntry]:
        entries: list[TimelineEntry] = []
        for app in self._journey.list_applications(customer_id):
            amount = app.approved_amount_cents or app.requested_amount_cents
            entries.append(
                TimelineEntry(
                    category=TimelineCategory.APPLICATION,
                    entry_date=app.event_date,
                    title=f"{app.product_applied} ({app.application_status.value})",
                    source_id=app.application_id,
                    amount_cents=int(amount) if amount is not None else None,
                )
            )
        return entries

    def _major_transaction_entries(self, customer_id: str) -> list[TimelineEntry]:
        transactions = self._journey.list_major_transactions(
            customer_id,
            absolute_threshold_cents=self._absolute_threshold_cents,
            median_multiple_bps=self._median_multiple_bps,
            limit=self._major_txn_limit,
        )
        return [
            TimelineEntry(
                category=TimelineCategory.MAJOR_TRANSACTION,
                entry_date=txn.transaction_date,
                title=f"{txn.transaction_category}: {txn.merchant or txn.transaction_type}",
                source_id=txn.transaction_id,
                amount_cents=int(txn.amount_cents),
            )
            for txn in transactions
        ]

    def _relationship_entries(self, customer_id: str) -> list[TimelineEntry]:
        """Relationship changes with a date. A relationship carries an ``as_of_date`` observation
        rather than a change date, so it is placed at its as-of; that is the date the platform knows
        the relationship to have held, which is what the timeline can honestly assert."""
        entries: list[TimelineEntry] = []
        for rel in self._relationship.list_relationships(customer_id):
            direction = "with" if rel.is_subject(customer_id) else "from"
            counterparty = rel.counterparty_of(customer_id)
            entries.append(
                TimelineEntry(
                    category=TimelineCategory.RELATIONSHIP_CHANGE,
                    entry_date=rel.as_of_date,
                    title=f"{rel.relationship_type.value} {direction} {counterparty}",
                    source_id=rel.relationship_id,
                )
            )
        return entries


__all__ = ["JourneyService", "TimelineCategory", "TimelineEntry"]
