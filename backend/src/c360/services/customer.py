"""``CustomerService`` — profile, contact and FTS search (task 5.1, design §6.1).

The service is a thin composition over :class:`~c360.domain.ports.CustomerRepository`. It adds three
things the repository deliberately does not: cursor pagination over the keyset the repository
exposes, the free-text-to-FTS query translation, and per-user recently-viewed tracking.

Search is entitlement-scoped *in the query*, not after it (requirement 3.3). The service hands the
principal's scope straight to :meth:`SqliteCustomerRepository.search_scoped`, so a restricted book's
result set is bounded by the book before ``LIMIT`` and its count can never betray a customer outside
it. The service never sees the identifier columns the index matched on — a hit on a card's last four
digits comes back as a :class:`~c360.domain.models.CustomerSearchHit`, not as the digits — so
nothing
maskable rides the search path.
"""

from __future__ import annotations

import contextlib
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from c360.api.pagination import Page, decode_cursor, encode_cursor
from c360.data.repositories.customer import build_fts_query
from c360.domain.enums import DelinquencyStatus, RiskBand

if TYPE_CHECKING:
    from c360.data.repositories.customer import SqliteCustomerRepository
    from c360.domain.enums import CustomerSegment, CustomerValue
    from c360.domain.models import (
        ContactInfo,
        Customer,
        CustomerCohortHit,
        CustomerSearchHit,
        Employer,
        Household,
    )
    from c360.security.entitlement import EntitlementScope


#: Band-score boundaries, matching :data:`c360.services.risk._BAND_*_MIN` and the repository's CASE.
#: A requested band becomes a half-open ``[min, max)`` score window the repository filters on.
_BAND_RANGES: dict[RiskBand, tuple[float | None, float | None]] = {
    RiskBand.LOW: (None, 25.0),
    RiskBand.MODERATE: (25.0, 50.0),
    RiskBand.ELEVATED: (50.0, 75.0),
    RiskBand.HIGH: (75.0, None),
}

#: The delinquency buckets that count as "past due" — every status except ``CURRENT``.
_DELINQUENT_STATUSES: tuple[DelinquencyStatus, ...] = (
    DelinquencyStatus.DPD_1_29,
    DelinquencyStatus.DPD_30_59,
    DelinquencyStatus.DPD_60_89,
    DelinquencyStatus.DPD_90_PLUS,
)


def _risk_band_range(band: RiskBand | None) -> tuple[float | None, float | None]:
    """The half-open ``[min, max)`` score window for a band, ``(None, None)`` when no band given."""
    if band is None:
        return None, None
    return _BAND_RANGES[band]


class RecentlyViewedTracker:
    """Per-user most-recently-viewed customer IDs (requirement 3.6).

    Bounded, in-memory and process-local: recently-viewed is a convenience for the current session,
    not a durable record (the audit log is the durable record of who viewed what, task 4.6). A
    per-user ring keeps the newest ``max_per_user`` ids, most-recent first, deduplicated so
    re-viewing a customer moves it to the front rather than adding a duplicate. Access is guarded by
    a lock because the aggregator's thread pool and the request threads touch it concurrently.
    """

    __slots__ = ("_by_user", "_lock", "_max_per_user", "_max_users")

    def __init__(self, *, max_per_user: int = 10, max_users: int = 1024) -> None:
        if max_per_user < 1:
            raise ValueError(f"max_per_user must be at least 1, got {max_per_user}")
        if max_users < 1:
            raise ValueError(f"max_users must be at least 1, got {max_users}")
        self._max_per_user = max_per_user
        self._max_users = max_users
        # An LRU of users so a long-lived process cannot grow unbounded in the number of users seen.
        self._by_user: OrderedDict[str, deque[str]] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, user_id: str, customer_id: str) -> None:
        """Note that ``user_id`` viewed ``customer_id``, moving it to the front of their ring."""
        with self._lock:
            ring = self._by_user.get(user_id)
            if ring is None:
                ring = deque(maxlen=self._max_per_user)
                self._by_user[user_id] = ring
            else:
                # Re-viewing moves to front rather than duplicating.
                with contextlib.suppress(ValueError):
                    ring.remove(customer_id)
            ring.appendleft(customer_id)
            self._by_user.move_to_end(user_id)
            while len(self._by_user) > self._max_users:
                self._by_user.popitem(last=False)

    def recent(self, user_id: str) -> tuple[str, ...]:
        """The user's recently-viewed customer IDs, most recent first."""
        with self._lock:
            ring = self._by_user.get(user_id)
            return tuple(ring) if ring is not None else ()


class CustomerService:
    """Search, profile and contact reads over the customer repository."""

    __slots__ = ("_recent", "_repository")

    def __init__(
        self,
        repository: SqliteCustomerRepository,
        *,
        recently_viewed: RecentlyViewedTracker | None = None,
    ) -> None:
        self._repository = repository
        self._recent = recently_viewed or RecentlyViewedTracker()

    @property
    def repository(self) -> SqliteCustomerRepository:
        """The underlying customer repository, for the authorization gate's scoped check."""
        return self._repository

    # ---------------------------------------------------------------- search (req 3.1, 3.3)
    def search(
        self,
        scope: EntitlementScope,
        query: str,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> Page[CustomerSearchHit]:
        """Entitlement-scoped FTS search, cursor-paginated (requirements 3.1, 3.3).

        A query that is empty or only punctuation returns an empty page rather than an error: a
        search box that has been cleared is not a client mistake to report, it is just no results.
        The page requests one more row than asked for to decide whether a ``next_cursor`` is owed,
        then trims — a keyset "is there a next page" that needs no separate count query.
        """
        match = build_fts_query(query)
        if match is None:
            return Page(items=[], next_cursor=None)
        after = decode_cursor(cursor)
        hits = self._repository.search_scoped(scope, match, limit=limit + 1, after=after)
        return _to_page(hits, limit, key=lambda hit: hit.customer_id)

    # ---------------------------------------------------------------- cohort (cross-customer Q&A)
    def cohort(
        self,
        scope: EntitlementScope,
        *,
        risk_band: RiskBand | None = None,
        min_risk_band: RiskBand | None = None,
        segments: Sequence[CustomerSegment] | None = None,
        values: Sequence[CustomerValue] | None = None,
        delinquent_only: bool = False,
        limit: int,
    ) -> Sequence[CustomerCohortHit]:
        """A ranked, entitlement-scoped list of the book matching a cohort criterion.

        This is the book-level counterpart to :meth:`search`: where search resolves *which* customer
        a named question is about, this answers a question about a *set* of customers — "high risk
        customers", "my platinum clients", "anyone past due". A requested :class:`RiskBand` is
        translated here into the ``risk_score`` range the repository filters on (the banding lives
        in the risk module; the service only maps a band to its cut points), so the repository stays
        a pure SQL filter and the band boundaries are defined once per layer. ``delinquent_only``
        to the four non-``CURRENT`` delinquency buckets. Ranking is by risk score descending when a
        risk band was asked for (highest-risk first), by value score otherwise. Scoping happens
        inside the query, so a restricted book never sees a customer outside it (requirement 3.3).

        ``risk_band`` and ``min_risk_band`` express two different asks: an exact band ("moderate
        risk customers") and a floor ("high risk" as most people mean it — the riskiest, i.e. this
        band or higher). An exact band wins when both are given; a floor drops the upper bound so
        every customer at or above the band's lower cut point is included.
        """
        if risk_band is not None:
            min_score, max_score = _risk_band_range(risk_band)
        elif min_risk_band is not None:
            min_score, max_score = _risk_band_range(min_risk_band)[0], None
        else:
            min_score, max_score = None, None
        wants_risk_order = risk_band is not None or min_risk_band is not None or delinquent_only
        delinquency_statuses = _DELINQUENT_STATUSES if delinquent_only else None
        return self._repository.cohort_scoped(
            scope,
            min_risk_score=min_score,
            max_risk_score=max_score,
            segments=list(segments) if segments else None,
            values=list(values) if values else None,
            delinquency_statuses=delinquency_statuses,
            order_by_risk_desc=wants_risk_order,
            limit=limit,
        )

    # ---------------------------------------------------------------- profile (req 4.5, 4.6)
    def get_profile(self, customer_id: str) -> Customer | None:
        """The customer profile, or ``None`` if the row does not exist.

        Authorization is the caller's responsibility and happens *before* this call (design §7.2);
        a service method never conflates "not entitled" with "does not exist".
        """
        return self._repository.get(customer_id)

    def get_contact(self, customer_id: str) -> ContactInfo | None:
        return self._repository.get_contact_info(customer_id)

    def get_employer(self, customer_id: str) -> Employer | None:
        return self._repository.get_employer_for_customer(customer_id)

    def get_household(self, household_id: str) -> Household | None:
        return self._repository.get_household(household_id)

    # ---------------------------------------------------------------- recently viewed (req 3.6)
    def record_view(self, user_id: str, customer_id: str) -> None:
        """Record that ``user_id`` viewed ``customer_id`` for the recently-viewed list."""
        self._recent.record(user_id, customer_id)

    def recently_viewed(self, user_id: str) -> tuple[str, ...]:
        return self._recent.recent(user_id)


def _to_page[ItemT](
    fetched: Sequence[ItemT],
    limit: int,
    *,
    key: Callable[[ItemT], str],
) -> Page[ItemT]:
    """Trim an over-fetched result to ``limit`` and derive the next cursor.

    ``fetched`` was read with ``limit + 1``: if it is longer than ``limit`` there is another page,
    and the cursor is the last *kept* item's key. Encoding is what makes the token opaque; the key
    itself is the repository's keyset value.
    """
    has_more = len(fetched) > limit
    items = list(fetched[:limit])
    next_cursor = encode_cursor(key(items[-1])) if has_more and items else None
    return Page(items=items, next_cursor=next_cursor)
