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
    optional_model,
    to_models,
)
from c360.domain.models import ContactInfo, Customer, CustomerSearchHit, Employer, Household
from c360.security.entitlement import customer_predicate

if TYPE_CHECKING:
    from collections.abc import Sequence

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
    return " ".join(f'"{token}"*' for token in tokens)


_COUNT = Statement(
    id="customer.count",
    collection="customer",
    sql="SELECT COUNT(*) FROM customer",
)


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
