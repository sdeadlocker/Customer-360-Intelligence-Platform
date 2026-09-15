"""``RelationshipService`` — relationships, household and graph (task 5.3, design §6.1, §5.4).

Three repositories meet here: the relational relationship reads (members, parties, beneficiaries,
assets, stored relationships), the graph traversal (neighborhood, path, household subgraph, degree),
and the customer repository — the last both for the household rollups and for the entitlement check
the graph redaction filter needs.

Graph redaction (design §5.4)
-----------------------------

A traversal naturally reaches customers outside the caller's book. Requirement 6.4 keeps such a node
*present* as structure while withholding its identity, and design §5.4 puts that filter in the
service layer so the UI and the Q&A agent get identical treatment. So every method that returns a
:class:`~c360.domain.graph.GraphView` passes it through :func:`redact_view` with a visibility check
closed over the principal — a node the principal may not see comes back as a restricted anchor, its
edges preserved. The traversal itself reads the true graph; redaction is the last step before the
view leaves the service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from c360.domain.enums import AccountStatus
from c360.domain.graph import GraphView
from c360.domain.money import Cents
from c360.security.entitlement import AllScope, BookScope
from c360.security.graph_redaction import redact_view

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.data.repositories.customer import SqliteCustomerRepository
    from c360.data.repositories.financial import SqliteFinancialRepository
    from c360.data.repositories.graph import SqliteGraphRepository
    from c360.data.repositories.relationship import SqliteRelationshipRepository
    from c360.domain.models import (
        AccountParty,
        AssetHolding,
        Beneficiary,
        CustomerRelationship,
        Household,
        HouseholdMember,
    )
    from c360.security.entitlement import EntitlementScope

#: The projection's node-id form for a customer (``CUSTOMER:C00042``); see the graph projection.
_CUSTOMER_NODE_PREFIX = "CUSTOMER"


def customer_node_id(customer_id: str) -> str:
    """The graph node id for a customer, matching the projection's typed composite key."""
    return f"{_CUSTOMER_NODE_PREFIX}:{customer_id}"


def scope_permits(
    scope: EntitlementScope, customer_id: str, customer: SqliteCustomerRepository
) -> bool:
    """Whether ``scope`` permits ``customer_id``, across all three scope variants.

    ``ALL`` and ``BOOK`` decide from the id alone with no query. ``SEGMENT`` cannot — it depends on
    the customer's segment — so it defers to :meth:`SqliteCustomerRepository.visible_to`, the same
    exists-and-entitled check in SQL the 403/404 gate uses. Keeping the cheap variants query-free is
    what lets the graph filter memoize and stay inside the traversal budget.
    """
    if isinstance(scope, AllScope):
        return scope.permits(customer_id)
    if isinstance(scope, BookScope):
        return scope.permits(customer_id)
    return customer.visible_to(scope, customer_id)


@dataclass(frozen=True, slots=True)
class HouseholdRollup:
    """Aggregate figures for a household (requirement 6.3).

    All monetary values are exact integer cents summed across members. ``product_count`` is the
    number of open accounts across the household; ``member_count`` is the number of members the
    rollup was computed over, so a caller can see the denominator behind the totals.
    """

    household_id: str
    member_count: int
    net_worth_cents: Cents
    total_deposits_cents: Cents
    product_count: int


class RelationshipService:
    """Relational relationship reads, household rollups and redacted graph traversal."""

    __slots__ = ("_customer", "_financial", "_graph", "_max_hops", "_node_cap", "_relationship")

    def __init__(
        self,
        relationship: SqliteRelationshipRepository,
        graph: SqliteGraphRepository,
        customer: SqliteCustomerRepository,
        financial: SqliteFinancialRepository,
        *,
        max_hops: int,
        node_cap: int,
    ) -> None:
        self._relationship = relationship
        self._graph = graph
        self._customer = customer
        self._financial = financial
        self._max_hops = max_hops
        self._node_cap = node_cap

    # ---------------------------------------------------------------- relational (req 6.1)
    def get_relationships(self, customer_id: str) -> Sequence[CustomerRelationship]:
        return self._relationship.list_relationships(customer_id)

    def get_beneficiaries(self, customer_id: str) -> Sequence[Beneficiary]:
        return self._relationship.list_beneficiaries(customer_id)

    def get_joint_accounts(self, customer_id: str) -> Sequence[AccountParty]:
        return self._relationship.list_joint_accounts(customer_id)

    def get_assets(self, customer_id: str) -> Sequence[AssetHolding]:
        return self._relationship.list_assets(customer_id)

    # ---------------------------------------------------------------- household (req 6.3, 6.5)
    def get_household(self, customer_id: str) -> Household | None:
        household_id = self._relationship.get_household_id_for_customer(customer_id)
        if household_id is None:
            return None
        return self._customer.get_household(household_id)

    def get_household_members(self, household_id: str) -> Sequence[HouseholdMember]:
        return self._relationship.list_household_members(household_id)

    def get_household_rollup(self, customer_id: str) -> HouseholdRollup | None:
        """Net worth, deposits and product count summed across the household (requirement 6.3).

        Returns ``None`` for a customer with no household — the isolated cohort — rather than a
        zero-filled rollup, so a caller renders "no household" instead of "a household worth $0".
        """
        household_id = self._relationship.get_household_id_for_customer(customer_id)
        if household_id is None:
            return None
        members = self._relationship.list_household_members(household_id)
        net_worth = Cents(0)
        deposits = Cents(0)
        product_count = 0
        for member in members:
            profile = self._financial.get_financial_profile(member.customer_id)
            if profile is not None:
                net_worth += profile.net_worth_cents
                deposits += profile.total_deposits_cents
            product_count += self._open_product_count(member.customer_id)
        return HouseholdRollup(
            household_id=household_id,
            member_count=len(members),
            net_worth_cents=net_worth,
            total_deposits_cents=deposits,
            product_count=product_count,
        )

    # ---------------------------------------------------------------- graph (req 6.2, 6.7)
    def get_network(
        self,
        scope: EntitlementScope,
        customer_id: str,
        *,
        edge_types: frozenset[str] | None = None,
    ) -> GraphView:
        """The customer's neighborhood, redacted for entitlement (requirements 6.2, 6.4, 6.7)."""
        view = self._graph.neighborhood(
            customer_node_id(customer_id),
            max_hops=self._max_hops,
            node_cap=self._node_cap,
            edge_types=edge_types,
        )
        return self._redact(scope, view)

    def get_household_network(self, scope: EntitlementScope, customer_id: str) -> GraphView | None:
        """The precomputed household subgraph, redacted. ``None`` when the customer has no
        household."""
        household_id = self._relationship.get_household_id_for_customer(customer_id)
        if household_id is None:
            return None
        return self._redact(scope, self._graph.household_subgraph(household_id))

    def path_between(self, source_customer_id: str, target_customer_id: str) -> tuple[str, ...]:
        """A shortest node-id path between two customers, capped at the configured hop limit."""
        return self._graph.path_between(
            customer_node_id(source_customer_id),
            customer_node_id(target_customer_id),
            max_hops=self._max_hops,
        )

    def degree_centrality(self, customer_id: str) -> int:
        return self._graph.degree_centrality(customer_node_id(customer_id))

    # ---------------------------------------------------------------- internals
    def _redact(self, scope: EntitlementScope, view: GraphView) -> GraphView:
        """Reduce non-entitled customer nodes to structure-only (design §5.4).

        Visibility is the scope's own decision (:func:`scope_permits`), which handles all three
        scope
        variants — an ``ALL`` or ``BOOK`` scope answers from the customer id alone, and a
        ``SEGMENT``
        scope resolves the customer's segment through the repository. Results are memoized per view
        so
        a node that appears on several edges is checked once, keeping a 300-node neighborhood from
        issuing 300 lookups.
        """
        cache: dict[str, bool] = {}

        def _visible(entity_id: str) -> bool:
            if entity_id not in cache:
                cache[entity_id] = scope_permits(scope, entity_id, self._customer)
            return cache[entity_id]

        return redact_view(view, _visible)

    def _open_product_count(self, customer_id: str) -> int:
        holdings = self._financial.list_holdings(customer_id, statuses=[AccountStatus.ACTIVE])
        return len(holdings)


__all__ = ["HouseholdRollup", "RelationshipService", "customer_node_id"]
