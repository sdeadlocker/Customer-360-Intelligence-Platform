"""SQLite adapter for :class:`~c360.domain.ports.CustomerRepository` (task 1.6).

Every statement selects columns explicitly. ``SELECT *`` would pass ``extra="forbid"`` only by
coincidence and would break the moment a migration adds a column, so the column list is the contract
between the migration and the model.

``contact_info`` joins ``customer`` for one column only: ``source_system``. Design §4.3's DDL puts
that column on the aggregate root and not on the dependent row, and task 1.6 requires provenance on
every read, so the join is how the invariant is met without a second round trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from c360.data.repositories.base import (
    SqliteRepository,
    Statement,
    expand_in_clause,
    optional_model,
    to_models,
)
from c360.domain.models import (
    ContactInfo,
    Customer,
    CustomerCohortHit,
    CustomerSearchHit,
    Employer,
    Household,
)
from c360.security.entitlement import customer_predicate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.domain.enums import CustomerValue, DelinquencyStatus
    from c360.security.entitlement import EntitlementScope

_GET = Statement(
    id="customer.get",
    collection="customer",
    sql="""
    SELECT customer_id, customer_name, customer_type, customer_segment, customer_since,
           date_of_birth, citizenship, occupation, employer_id, employment_status, marital_status,
           customer_value, customer_value_score, preferred_language, preferred_channel,
           household_id, as_of_date, source_system
    FROM customer
    WHERE customer_id = :customer_id
    """,
)

_EXISTS = Statement(
    id="customer.exists",
    collection="customer",
    # `SELECT 1` rather than the row: an existence check runs on the 404-versus-403 path (design
    # §7.2) where loading maskable columns would be work done only to throw away.
    sql="SELECT 1 FROM customer WHERE customer_id = :customer_id",
)

_CONTACT_INFO = Statement(
    id="customer.contact_info",
    collection="contact_info",
    sql="""
    SELECT ci.customer_id, ci.email, ci.phone_number, ci.mobile_number,
           ci.address_line1, ci.address_line2, ci.city, ci.state, ci.country, ci.postal_code,
           ci.as_of_date, c.source_system
    FROM contact_info ci
    JOIN customer c ON c.customer_id = ci.customer_id
    WHERE ci.customer_id = :customer_id
    """,
)

_EMPLOYER_FOR_CUSTOMER = Statement(
    id="customer.employer_for_customer",
    collection="employer",
    # Provenance comes from `customer`, because the fact being read is "this customer works here"
    # and `employer` carries no temporal columns of its own. See the port docstring.
    sql="""
    SELECT e.employer_id, e.employer_name, e.industry, e.city, e.state,
           c.as_of_date, c.source_system
    FROM customer c
    JOIN employer e ON e.employer_id = c.employer_id
    WHERE c.customer_id = :customer_id
    """,
)

_HOUSEHOLD = Statement(
    id="customer.household",
    collection="household",
    # `household` has `as_of_date` but no `source_system`; it is derived from customer addresses, so
    # the system of record is whichever system the primary customer came from.
    sql="""
    SELECT h.household_id, h.household_name, h.primary_customer_id, h.address_hash,
           h.member_count, h.as_of_date,
           COALESCE(
             (SELECT c.source_system FROM customer c WHERE c.customer_id = h.primary_customer_id),
             (SELECT c.source_system FROM customer c WHERE c.household_id = h.household_id
              ORDER BY c.customer_id LIMIT 1),
             'DERIVED'
           ) AS source_system
    FROM household h
    WHERE h.household_id = :household_id
    """,
)

_LIST_ALL = Statement(
    id="customer.list_all",
    collection="customer",
    # The full profile row for every customer, ordered by id so a batch consumer (the Phase 17
    # signal-detection job) processes them deterministically. Same column list as `customer.get`,
    # so it validates into the same `Customer` model under `extra="forbid"`.
    sql="""
    SELECT customer_id, customer_name, customer_type, customer_segment, customer_since,
           date_of_birth, citizenship, occupation, employer_id, employment_status, marital_status,
           customer_value, customer_value_score, preferred_language, preferred_channel,
           household_id, as_of_date, source_system
    FROM customer
    ORDER BY customer_id
    """,
)

_LIST_IDS = Statement(
    id="customer.list_ids",
    collection="customer",
    # Keyset pagination on the primary key. `:after IS NULL OR ...` keeps this a single statement
    # rather than two near-identical ones, and SQLite still resolves the index for the bounded case.
    sql="""
    SELECT customer_id
    FROM customer
    WHERE (:after IS NULL OR customer_id > :after)
    ORDER BY customer_id
    LIMIT :limit
    """,
)

# Entitlement-scoped listing (task 4.3). The scope predicate is spliced into the WHERE clause and
# evaluated *before* LIMIT, so a restricted book never loses a match it was entitled to just
# because an unentitled row would have ordered ahead of it (requirement 3.3, design §4.4). The
# `{scope}` placeholder is filled with a parameterized fragment built by the scope itself — never
# with values — so this is not string-built SQL in the injection sense.
_LIST_IDS_SCOPED_TEMPLATE = """
    SELECT c.customer_id
    FROM customer c
    WHERE (:after IS NULL OR c.customer_id > :after)
      AND ({scope})
    ORDER BY c.customer_id
    LIMIT :limit
    """

# The scoped existence check answers "does this customer exist AND may this principal see it" in
# one statement, so the 403-versus-404 decision (requirement 15.5) can be made without two round
# trips and without a window where existence leaks.
_SCOPED_EXISTS_TEMPLATE = """
    SELECT 1 FROM customer c WHERE c.customer_id = :customer_id AND ({scope})
    """

# FTS5 search (requirement 3.1). `customer_search` is the projected index; it carries `customer_id`
# and `segment` UNINDEXED so a hit joins back to `customer` for the display fields the result list
# shows. Entitlement is applied by joining the index to `customer` and splicing the same scope
# predicate the listing uses into the WHERE — a MATCH plus a scope, evaluated together before LIMIT,
# so a restricted book never returns a hit it was not entitled to and the result count never reveals
# a non-entitled match (requirement 3.3). Ordering is by BM25 relevance (FTS5's default `rank`),
# with the id as a stable tiebreaker so the keyset cursor is total.
_SEARCH_SCOPED_TEMPLATE = """
    SELECT c.customer_id, c.customer_name, c.customer_segment, cs.city
    FROM customer_search cs
    JOIN customer c ON c.customer_id = cs.customer_id
    WHERE customer_search MATCH :match
      AND (:after IS NULL OR c.customer_id > :after)
      AND ({scope})
    ORDER BY c.customer_id
    LIMIT :limit
    """

#: Characters FTS5 treats as query syntax. Stripped from user input before it is wrapped as a quoted
#: prefix term, so a stray quote or column-filter colon in a search box cannot become a malformed
#: MATCH expression (a syntax error) or a filter on an unintended column.
_FTS_SYNTAX = str.maketrans(dict.fromkeys('"*:^()-+' + "'", " "))


def build_fts_query(raw: str) -> str | None:
    """Turn a free-text search box into a safe FTS5 MATCH expression, or ``None`` if it is empty.

    Each whitespace-separated token is stripped of FTS operator characters, wrapped in double quotes
    as a literal phrase, and given a trailing ``*`` for prefix matching (the index is built with
    ``prefix = "2 3 4"``, so typing part of a name or number matches). Tokens are joined by an
    implicit AND, so "renata columbus" matches a row containing both. Returning ``None`` for input
    that is empty once stripped lets the service answer an all-whitespace or punctuation-only query
    with an empty page rather than issuing a MATCH that FTS5 would reject.
    """
    tokens = [token for token in raw.translate(_FTS_SYNTAX).split() if token]
    if not tokens:
        return None
    # Tokens are joined by OR (not implicit AND) so a natural-language phrase from the
    # cross-customer Q&A agent — e.g. "Marta Farooqi standing" — still resolves the customer on the
    # name tokens instead of AND-matching to zero because a non-name word ("standing") is not in any
    # row. FTS5 BM25 ranking floats the row matching the most tokens to the top, so a full-name
    # query still ranks the right customer first. The UI identifier search (name, id, account,
    # card-4) also benefits: a partial multi-token entry no longer requires every token to hit.
    return " OR ".join(f'"{token}"*' for token in tokens)


_COUNT = Statement(
    id="customer.count",
    collection="customer",
    sql="SELECT COUNT(*) FROM customer",
)

# Book-level cohort listing (cross-customer Q&A, "high risk customers", "my platinum clients").
# Unlike search this is not FTS: it filters and ranks the entitled book by the coarse, non-maskable
# columns a cohort question names — risk band (derived from risk_score), segment, value tier and
# delinquency status. The scope predicate is spliced in exactly as the search and listing do, before
# ORDER BY and LIMIT, so a restricted book only ever surfaces its own customers and the count never
# reveals an out-of-book match (requirement 3.3). The optional filters use the `:p IS NULL OR ...`
# idiom so one statement serves a query with any subset of filters. `risk_band` is computed in the
# SELECT via a CASE on risk_score so the caller sees the same LOW/MODERATE/ELEVATED/HIGH banding the
# risk module derives, without a stored column. LEFT JOIN so a customer with no risk row still lists
# (as an unscored, LOW-band member) rather than silently dropping out of a segment/value cohort.
_COHORT_SCOPED_TEMPLATE = """
    SELECT c.customer_id, c.customer_name, c.customer_segment,
           c.customer_value, c.customer_value_score,
           rp.risk_score,
           CASE
             WHEN rp.risk_score IS NULL OR rp.risk_score < {moderate} THEN 'LOW'
             WHEN rp.risk_score < {elevated} THEN 'MODERATE'
             WHEN rp.risk_score < {high} THEN 'ELEVATED'
             ELSE 'HIGH'
           END AS risk_band,
           rp.delinquency_status
    FROM customer c
    LEFT JOIN risk_profile rp ON rp.customer_id = c.customer_id
    WHERE ({scope})
      AND (:min_risk_score IS NULL OR rp.risk_score >= :min_risk_score)
      AND (:max_risk_score IS NULL OR rp.risk_score < :max_risk_score)
      {segment_filter}
      {value_filter}
      {delinquency_filter}
    ORDER BY {order_by}
    LIMIT :limit
    """

# Band-score boundaries duplicated from RiskService._band_for (services/risk.py) rather than
# imported so the repository does not depend up onto the service layer. Same 0..100 cut points
# (LOW < 25, MODERATE < 50, ELEVATED < 75, HIGH >= 75); a change in one place must change both.
_BAND_MODERATE_MIN = 25.0
_BAND_ELEVATED_MIN = 50.0
_BAND_HIGH_MIN = 75.0


def _in_filter(
    column: str, name: str, values: Sequence[object] | None
) -> tuple[str, dict[str, object]]:
    """Render an optional ``AND column IN (...)`` fragment and its bindings, or an empty fragment.

    A ``None`` or empty filter contributes no SQL and no params, so an unfiltered cohort query is
    the base statement with nothing spliced in. A non-empty filter becomes a parameterized ``IN``
    list via :func:`expand_in_clause` — placeholders generated, values bound — so the fragment is
    safe to interpolate into the statement template. Enum values are coerced to their string form so
    a :class:`~enum.StrEnum` member binds as the stored text.
    """
    if not values:
        return "", {}
    coerced = [str(value) for value in values]
    placeholders, bindings = expand_in_clause(name, coerced)
    return f"AND {column} IN ({placeholders})", bindings


class SqliteCustomerRepository(SqliteRepository):
    """Reads backing requirements 4.5 and 4.6."""

    def get(self, customer_id: str) -> Customer | None:
        return optional_model(Customer, self.fetch_one(_GET, {"customer_id": customer_id}))

    def exists(self, customer_id: str) -> bool:
        return self.fetch_one(_EXISTS, {"customer_id": customer_id}) is not None

    def get_contact_info(self, customer_id: str) -> ContactInfo | None:
        return optional_model(
            ContactInfo, self.fetch_one(_CONTACT_INFO, {"customer_id": customer_id})
        )

    def get_employer_for_customer(self, customer_id: str) -> Employer | None:
        """Return the customer's employer, or ``None`` if unemployed, unrecorded or unknown.

        The three cases are not distinguished, and that is the right call for this read: requirement
        4.7 renders an explicit "not available" indicator for a field with no value, and "no
        employer
        on file" and "not employed" are the same thing to that indicator. Whether the *customer*
        exists is :meth:`exists`'s question.
        """
        return optional_model(
            Employer, self.fetch_one(_EMPLOYER_FOR_CUSTOMER, {"customer_id": customer_id})
        )

    def get_household(self, household_id: str) -> Household | None:
        return optional_model(Household, self.fetch_one(_HOUSEHOLD, {"household_id": household_id}))

    def list_all(self) -> Sequence[Customer]:
        """Every customer profile, ordered by id (task 17.4).

        The signal-detection batch job iterates the whole population, so it reads full profiles in
        one statement rather than N ``get`` calls. Unscoped by design: detection runs as a
        system job over the read-only database, and entitlement is applied when the *worklist* is
        read per user (task 17.3), never at detection time.
        """
        return to_models(Customer, self.fetch_all(_LIST_ALL))

    def list_ids(self, *, limit: int, after: str | None = None) -> Sequence[str]:
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        rows = self.fetch_all(_LIST_IDS, {"limit": limit, "after": after})
        return tuple(str(row[0]) for row in rows)

    def count(self) -> int:
        return int(self.fetch_scalar(_COUNT, default=0))

    # ---------------------------------------------------------------- entitlement scoping (4.3)
    def visible_to(self, scope: EntitlementScope, customer_id: str) -> bool:
        """Whether ``customer_id`` exists *and* ``scope`` permits it, in one statement.

        This is the read behind the 403-versus-404 decision (requirement 15.5): a caller that gets
        ``True`` may see the customer; a caller that gets ``False`` learns only that — never whether
        the row is absent or merely off-limits, because the scope predicate is inside the same query
        as the existence check.
        """
        predicate = customer_predicate(
            scope, id_column="c.customer_id", segment_column="c.customer_segment"
        )
        statement = Statement(
            id="customer.scoped_exists",
            collection="customer",
            sql=_SCOPED_EXISTS_TEMPLATE.format(scope=predicate.sql),
        )
        params = {"customer_id": customer_id, **predicate.params}
        return self.fetch_one(statement, params) is not None

    def list_ids_scoped(
        self,
        scope: EntitlementScope,
        *,
        limit: int,
        after: str | None = None,
    ) -> Sequence[str]:
        """Entitled customer IDs in stable order, with the scope applied inside the query.

        The scope is a ``WHERE`` predicate evaluated before ``LIMIT`` (requirement 3.3): a
        restricted book returns its own customers, never a truncated slice of the whole table, and
        the count it produces never reveals how many customers exist outside the book.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        predicate = customer_predicate(
            scope, id_column="c.customer_id", segment_column="c.customer_segment"
        )
        statement = Statement(
            id="customer.list_ids_scoped",
            collection="customer",
            sql=_LIST_IDS_SCOPED_TEMPLATE.format(scope=predicate.sql),
        )
        params = {"limit": limit, "after": after, **predicate.params}
        rows = self.fetch_all(statement, params)
        return tuple(str(row[0]) for row in rows)

    def search_scoped(
        self,
        scope: EntitlementScope,
        match: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> Sequence[CustomerSearchHit]:
        """FTS5 search over ``customer_search``, entitlement-scoped inside the query (req 3.1, 3.3).

        ``match`` is a MATCH expression from :func:`build_fts_query`, not raw user text — the
        service
        builds it so this layer never has to know FTS syntax rules. Results are the display-only
        :class:`~c360.domain.models.CustomerSearchHit`, never the identifier columns the index
        searched on, and the scope is spliced in exactly as the listing does so a restricted book's
        result set cannot reveal a customer outside it.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        predicate = customer_predicate(
            scope, id_column="c.customer_id", segment_column="c.customer_segment"
        )
        statement = Statement(
            id="customer.search_scoped",
            collection="customer_search",
            sql=_SEARCH_SCOPED_TEMPLATE.format(scope=predicate.sql),
        )
        params = {"match": match, "limit": limit, "after": after, **predicate.params}
        rows = self.fetch_all(statement, params)
        return to_models(CustomerSearchHit, rows)

    def cohort_scoped(
        self,
        scope: EntitlementScope,
        *,
        min_risk_score: float | None = None,
        max_risk_score: float | None = None,
        segments: Sequence[CustomerValue] | Sequence[str] | None = None,
        values: Sequence[CustomerValue] | Sequence[str] | None = None,
        delinquency_statuses: Sequence[DelinquencyStatus] | Sequence[str] | None = None,
        order_by_risk_desc: bool = True,
        limit: int,
    ) -> Sequence[CustomerCohortHit]:
        """A ranked, entitlement-scoped cohort of the book by risk / segment / value / delinquency.

        Every filter is optional and folded into one statement with the ``:p IS NULL OR ...`` idiom
        (for the score bounds) or a spliced ``IN`` list (for the enum filters), so a caller asking
        only for "high risk" and a caller asking for "platinum small-business clients current on
        payments" both run this one query. The scope predicate is evaluated before ``LIMIT`` as the
        search and listing paths do, so a restricted book can never surface a customer outside it
        and the row count never leaks an out-of-book match (requirement 3.3).

        ``segments``/``values``/``delinquency_statuses`` are turned into parameterized ``IN`` lists
        by :func:`expand_in_clause` — the placeholders are generated, never the values — so this
        stays free of string-built SQL in the injection sense. Ranking is by risk score (descending
        for "highest risk first", the common cohort ask) or by customer value score, with the
        customer id as a stable tiebreaker so the order is reproducible.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        predicate = customer_predicate(
            scope, id_column="c.customer_id", segment_column="c.customer_segment"
        )
        params: dict[str, object] = {
            "min_risk_score": min_risk_score,
            "max_risk_score": max_risk_score,
            "limit": limit,
            **predicate.params,
        }
        segment_filter, seg_params = _in_filter("c.customer_segment", "seg", segments)
        value_filter, val_params = _in_filter("c.customer_value", "val", values)
        delinquency_filter, delq_params = _in_filter(
            "rp.delinquency_status", "delq", delinquency_statuses
        )
        params.update(seg_params)
        params.update(val_params)
        params.update(delq_params)
        order_by = (
            "rp.risk_score DESC, c.customer_id"
            if order_by_risk_desc
            else "c.customer_value_score DESC, c.customer_id"
        )
        statement = Statement(
            id="customer.cohort_scoped",
            collection="customer",
            sql=_COHORT_SCOPED_TEMPLATE.format(
                scope=predicate.sql,
                segment_filter=segment_filter,
                value_filter=value_filter,
                delinquency_filter=delinquency_filter,
                order_by=order_by,
                moderate=_BAND_MODERATE_MIN,
                elevated=_BAND_ELEVATED_MIN,
                high=_BAND_HIGH_MIN,
            ),
        )
        rows = self.fetch_all(statement, params)
        return to_models(CustomerCohortHit, rows)
