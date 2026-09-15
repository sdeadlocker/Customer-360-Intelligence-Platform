"""Entitlement scope — the row-level authorization gate (task 4.3).

Design §7.2 draws two independent gates. This module is the first: *whether the principal may see
this customer at all*. The second, field-level masking, is :mod:`c360.security.policy`.

Three variants, from the design §7.1 ``Principal`` definition:

* :class:`AllScope` — every customer (a supervisor or platform role);
* :class:`BookScope` — a fixed book of business, an explicit set of customer IDs (requirement A7:
  "an assignable book of business per user");
* :class:`SegmentScope` — every customer in one or more segments.

The scope is applied **inside** the repository query, not as a post-filter over results. Design
§4.4 and requirement 3.3 are explicit about why: a restricted book must never *lose* a match it was
entitled to just because an unentitled row ranked ahead of it, and search must never reveal the
existence of a customer outside the book by returning a smaller count. A post-filter cannot express
either property — only a ``WHERE`` clause the database evaluates before ``LIMIT`` can. Each variant
therefore knows how to render itself as a SQL predicate over a customer-id column and the bound
parameters that go with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from c360.domain.enums import CustomerSegment

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class SqlPredicate:
    """A SQL boolean fragment plus its bound parameters.

    ``sql`` is spliced into a ``WHERE`` clause; ``params`` are bound, never interpolated. The
    parameter names are namespaced (``ent_``) so a predicate cannot collide with a caller's own
    bindings.
    """

    sql: str
    params: Mapping[str, object]


#: A predicate that always matches. ``1 = 1`` rather than an empty string so a caller can splice it
#: unconditionally into ``AND (...)`` without special-casing the unrestricted role.
_ALWAYS: Final = "1 = 1"

#: A predicate that never matches, for a scope that resolves to an empty set (an empty book).
_NEVER: Final = "1 = 0"


@dataclass(frozen=True, slots=True)
class AllScope:
    """Every customer. Carries no data because there is nothing to bound."""

    def permits(self, customer_id: str) -> bool:
        return True

    def predicate(self, column: str) -> SqlPredicate:
        return SqlPredicate(sql=_ALWAYS, params={})


@dataclass(frozen=True, slots=True)
class BookScope:
    """A fixed set of customer IDs — the user's assignable book of business (requirement A7)."""

    customer_ids: frozenset[str]

    def permits(self, customer_id: str) -> bool:
        return customer_id in self.customer_ids

    def predicate(self, column: str) -> SqlPredicate:
        if not self.customer_ids:
            return SqlPredicate(sql=_NEVER, params={})
        # Sorted so the generated placeholder order is deterministic — a test asserting the SQL is
        # stable across runs, and a query plan cache, both benefit.
        ids = sorted(self.customer_ids)
        names = [f"ent_book_{index}" for index in range(len(ids))]
        placeholders = ", ".join(f":{name}" for name in names)
        params = dict(zip(names, ids, strict=True))
        return SqlPredicate(sql=f"{column} IN ({placeholders})", params=params)


@dataclass(frozen=True, slots=True)
class SegmentScope:
    """Every customer whose segment is in the allowed set."""

    segments: frozenset[CustomerSegment]

    def permits_segment(self, segment: CustomerSegment) -> bool:
        return segment in self.segments

    def predicate(self, column: str) -> SqlPredicate:
        if not self.segments:
            return SqlPredicate(sql=_NEVER, params={})
        values = sorted(str(segment) for segment in self.segments)
        names = [f"ent_seg_{index}" for index in range(len(values))]
        placeholders = ", ".join(f":{name}" for name in names)
        params = dict(zip(names, values, strict=True))
        return SqlPredicate(sql=f"{column} IN ({placeholders})", params=params)


#: The scope union carried on a :class:`~c360.security.model.Principal`.
type EntitlementScope = AllScope | BookScope | SegmentScope


def customer_predicate(
    scope: EntitlementScope,
    *,
    id_column: str,
    segment_column: str,
) -> SqlPredicate:
    """Render ``scope`` as a predicate over a customer row, choosing the right column per variant.

    A :class:`BookScope` filters on the customer-id column; a :class:`SegmentScope` filters on the
    segment column; an :class:`AllScope` matches everything. Keeping the column choice here rather
    than in each query means a repository names its two columns once and never has to branch on the
    scope variant — and a new scope variant only has to be handled in this one function.
    """
    if isinstance(scope, SegmentScope):
        return scope.predicate(segment_column)
    if isinstance(scope, BookScope):
        return scope.predicate(id_column)
    return scope.predicate(id_column)


def scope_from_claim(kind: str, values: list[str] | None = None) -> EntitlementScope:
    """Build an :class:`EntitlementScope` from a token's entitlement claim.

    The local provider and the OIDC claim mapping both express entitlement as a ``kind`` plus an
    optional value list, so both go through here and cannot diverge. ``kind`` is matched
    case-insensitively because an IdP's claim casing is not something the platform controls.

    Raises:
        ValueError: ``kind`` is unknown, or a scope that requires values was given none.
    """
    normalized = kind.strip().upper()
    if normalized == "ALL":
        return AllScope()
    if normalized == "BOOK":
        return BookScope(customer_ids=frozenset(values or ()))
    if normalized == "SEGMENT":
        segments = frozenset(CustomerSegment(value) for value in (values or ()))
        return SegmentScope(segments=segments)
    raise ValueError(f"unknown entitlement scope kind: {kind!r}")
