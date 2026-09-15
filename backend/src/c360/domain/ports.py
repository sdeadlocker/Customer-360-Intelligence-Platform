"""Repository ports (task 1.6).

One :class:`~typing.Protocol` per aggregate. Design §2.2 makes the direction of dependency a rule:
services depend on these declarations, and :mod:`c360.data.repositories` supplies the SQLite
implementations. Nothing here mentions SQLAlchemy, SQL or a connection.

``Protocol`` rather than an abstract base class, deliberately. Structural typing means an
implementation does not import the port in order to satisfy it, so the test doubles that Phases 4-8
need — a repository that returns a fixed customer, a repository that raises to exercise the
aggregator's per-domain failure isolation — are plain classes with the right methods rather than
subclasses carrying an inheritance chain they have no use for. Each protocol is
``@runtime_checkable`` so a test can assert conformance without instantiating a database.

Why the split is per aggregate rather than one big repository
------------------------------------------------------------

Design §6.1 gives each application service its own boundary, and design §5.7's aggregator fans out
across those services concurrently so that one domain failing degrades one module instead of the
dashboard. Six narrow ports make that fan-out expressible and make "which reads does the risk module
perform" answerable by reading one protocol.

Read-only, on purpose
---------------------

There is no ``save``, ``insert`` or ``update`` anywhere in this module. Requirement A1 makes the
platform read-only over source data and design §12.3 opens the customer database ``mode=ro``. Writes
live in the seeder and the recompute job, which own their own write path; a mutating method here
would be a method that fails at runtime on every deployed instance.

Every method returns a fully-formed model or ``None`` — never a partially-populated object and never
a raw row. Entitlement scoping (task 4.3) is applied *inside* the implementations of these methods
rather than by a caller filtering afterwards, which is why several signatures take a scope argument
once that phase lands; the note is here so the eventual addition reads as planned rather than
bolted on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from c360.domain.enums import (
        AccountStatus,
        AccountType,
        EngagementChannel,
        EngagementEventType,
    )
    from c360.domain.graph import GraphView
    from c360.domain.models import (
        AccountParty,
        Application,
        AssetHolding,
        Beneficiary,
        Campaign,
        CategoryTotal,
        ContactInfo,
        CreditProfile,
        Customer,
        CustomerRelationship,
        Employer,
        EngagementEvent,
        FinancialProfile,
        Holding,
        Household,
        HouseholdMember,
        LifeEvent,
        MonthlyTotal,
        OfferForCustomer,
        RiskProfile,
        Transaction,
    )
    from c360.security.audit import AuditRecord
    from c360.security.model import Principal


@runtime_checkable
class CustomerRepository(Protocol):
    """Identity, profile and contact reads. Requirements 4.5, 4.6."""

    def get(self, customer_id: str) -> Customer | None:
        """Return the customer, or ``None`` if no such row exists.

        ``None`` means *does not exist*. It never means *not entitled*: requirement 12.3 and design
        §7.2 distinguish 403 from 404, and collapsing the two here would make that distinction
        impossible for the API layer to draw — or worse, would turn "not entitled" into a 404 that
        confirms non-existence for records the caller may not know about.
        """
        ...

    def exists(self, customer_id: str) -> bool:
        """Whether the customer exists, without loading or masking the row."""
        ...

    def get_contact_info(self, customer_id: str) -> ContactInfo | None: ...

    def get_employer_for_customer(self, customer_id: str) -> Employer | None:
        """Return the customer's employer, or ``None`` if unemployed or not recorded.

        Keyed on the customer rather than on ``employer_id``, which is not merely convenience.
        ``employer`` is the one table in design §4.3 with neither ``as_of_date`` nor
        ``source_system``, because it is reference data about an organization rather than an
        observation about a customer. The customer-level fact is ``customer.employer_id`` — "this
        customer works here" — and that row carries the as-of date requirement 4.8 displays and the
        system of record design §4.3 wants for citation. Reading the employer through the customer
        is
        what makes provenance available at all; reading it by ID would leave nothing honest to fill
        those fields with.

        Requirement 4.5 only ever needs the employer of a selected customer, so nothing is lost.
        """
        ...

    def get_household(self, household_id: str) -> Household | None: ...

    def list_ids(self, *, limit: int, after: str | None = None) -> Sequence[str]:
        """Customer IDs in stable order, for cursor pagination and for batch jobs.

        Ordering by primary key rather than by a display field is what makes ``after`` a usable
        cursor: it is total, stable across inserts, and needs no tiebreaker.
        """
        ...

    def count(self) -> int: ...


@runtime_checkable
class FinancialRepository(Protocol):
    """Balances, holdings and transaction analytics. Requirements 5.1-5.10."""

    def get_financial_profile(self, customer_id: str) -> FinancialProfile | None: ...

    def get_credit_profile(self, customer_id: str) -> CreditProfile | None: ...

    def list_holdings(
        self,
        customer_id: str,
        *,
        account_types: Sequence[AccountType] | None = None,
        statuses: Sequence[AccountStatus] | None = None,
    ) -> Sequence[Holding]:
        """All accounts with their product specializations. Requirement 5.3.

        Filters are applied in SQL, not by the caller, so a request for active deposits reads the
        ``ix_account_cust_type`` index instead of loading every holding to discard most of them.
        """
        ...

    def list_transactions(
        self,
        customer_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        categories: Sequence[str] | None = None,
        limit: int = 500,
    ) -> Sequence[Transaction]:
        """Transactions, most recent first, bounded by ``limit``.

        The bound is not optional. ``txn`` is the volume table and an unbounded read of it is the
        one query in this schema that can exceed the §12 latency budget on its own.
        """
        ...

    def totals_by_category(
        self,
        customer_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> Sequence[CategoryTotal]:
        """Spend aggregated by category. Requirement 5.8.

        Summed in SQL. Design §1.1's exactness guarantee holds either way since both sides are
        integers, but folding tens of thousands of rows in Python would spend the latency budget
        moving data rather than computing on it.
        """
        ...

    def monthly_totals(
        self,
        customer_id: str,
        *,
        category: str | None = None,
        months: int = 12,
    ) -> Sequence[MonthlyTotal]:
        """Month-by-month spend, oldest first. Requirements 5.8 and 5.9."""
        ...

    def median_transaction_amount_cents(self, customer_id: str) -> int | None:
        """Median absolute debit amount, or ``None`` when the customer has no debits.

        Requirement 7.4 defines a "major" transaction partly as a multiple of the customer's median
        transaction value, so the median has to come from the same place the transactions do.
        """
        ...


@runtime_checkable
class RiskRepository(Protocol):
    """Risk posture reads. Requirements 8.1-8.6."""

    def get_risk_profile(self, customer_id: str) -> RiskProfile | None: ...

    def total_credit_exposure_cents(self, customer_id: str) -> int:
        """Loan balances plus card limits. Design §4.6, requirement 8.5.

        Returns 0 rather than ``None`` for a customer with no credit: zero exposure is a fact, and
        requirement 4.7's "not available" indicator is for missing values, not for empty sums.
        """
        ...


@runtime_checkable
class RelationshipRepository(Protocol):
    """Household, party, beneficiary and asset reads. Requirement 6.1.

    Graph traversal is deliberately not here: it has its own port (design §5.3) over the projected
    ``graph_adjacency`` table, and it arrives with the projection in Phase 3.
    """

    def list_household_members(self, household_id: str) -> Sequence[HouseholdMember]: ...

    def get_household_id_for_customer(self, customer_id: str) -> str | None: ...

    def list_relationships(self, customer_id: str) -> Sequence[CustomerRelationship]:
        """Relationships where ``customer_id`` appears on either side, as stored.

        Deliberately *not* normalized so that ``customer_id`` is always the ``from`` side. Half of
        :class:`~c360.domain.enums.RelationshipType` is asymmetric — flipping a ``PARENT`` edge and
        keeping the label turns "A is the parent of B" into "B is the parent of A", and
        ``GUARDIAN``,
        ``REFERRED_BY`` and ``ADVISOR`` have no inverse member to flip to. Rows therefore come back
        in their stored direction, and
        :meth:`~c360.domain.models.CustomerRelationship.counterparty_of` resolves the other party
        for
        a caller that just wants the neighbour.
        """
        ...

    def list_account_parties(self, account_id: str) -> Sequence[AccountParty]: ...

    def list_joint_accounts(self, customer_id: str) -> Sequence[AccountParty]:
        """Every account this customer is a party to in a role other than sole primary holder."""
        ...

    def list_beneficiaries(self, customer_id: str) -> Sequence[Beneficiary]:
        """Beneficiaries named on any of this customer's accounts."""
        ...

    def list_assets(self, customer_id: str) -> Sequence[AssetHolding]: ...


@runtime_checkable
class OfferRepository(Protocol):
    """Offer and campaign reads. Requirements 9.1-9.7."""

    def list_offers_for_customer(
        self,
        customer_id: str,
        *,
        include_suppressed: bool = True,
    ) -> Sequence[OfferForCustomer]:
        """Offers joined to this customer's reaction, newest presentation first.

        ``include_suppressed`` defaults to ``True`` because requirement 9.5 requires a suppressed
        offer to still be *displayed*, de-emphasized and with its reason — excluding it from the
        read
        would make the requirement unimplementable above this layer.
        """
        ...

    def get_campaign(self, campaign_id: str) -> Campaign | None: ...

    def list_campaigns_for_customer(self, customer_id: str) -> Sequence[Campaign]:
        """Campaigns this customer is a member of, via the offers presented to them. Req 9.7."""
        ...


@runtime_checkable
class JourneyRepository(Protocol):
    """Timeline component reads. Requirements 7.1-7.6."""

    def list_life_events(self, customer_id: str) -> Sequence[LifeEvent]: ...

    def list_applications(self, customer_id: str) -> Sequence[Application]: ...

    def list_engagement_events(
        self,
        customer_id: str,
        *,
        channels: Sequence[EngagementChannel] | None = None,
        event_types: Sequence[EngagementEventType] | None = None,
        limit: int = 200,
    ) -> Sequence[EngagementEvent]:
        """Engagement history, newest first, filterable by channel and type. Requirement 7.6."""
        ...

    def list_major_transactions(
        self,
        customer_id: str,
        *,
        absolute_threshold_cents: int,
        median_multiple_bps: int,
        limit: int = 50,
    ) -> Sequence[Transaction]:
        """Transactions qualifying as "major" for the timeline. Requirement 7.4.

        A transaction qualifies if it clears the absolute threshold **or** the median multiple —
        either criterion, per task 5.6.

        Both criteria are passed in rather than read from configuration, because requirement 7.4
        makes them configurable and a repository that reads settings is a repository a test cannot
        pin down.

        The multiple is basis points, not a float, even though ``MAJOR_TXN_MEDIAN_MULTIPLE`` is
        configured as one. The comparison is against a monetary amount, and design §1.1 keeps
        floating point out of those; the service converts the configured value once
        (``10.0`` → ``100_000`` bps) rather than letting a float into the predicate.
        """
        ...


@runtime_checkable
class IdentityProvider(Protocol):
    """Authentication port (task 4.1, design §7.1).

    Two implementations satisfy this: a local OAuth 2.0 provider that issues RS256 JWTs from a
    seeded user table, and an OIDC adapter that validates an enterprise IdP's tokens via JWKS. Both
    produce the same :class:`~c360.security.model.Principal`, so nothing downstream learns which one
    is bound — which is exactly what makes the production swap (requirement 12.2) a configuration
    change rather than a code change.

    Only :meth:`authenticate` is universal. Token *issuance* and *refresh* are meaningful for the
    local provider and not for OIDC (an enterprise IdP issues its own tokens), so those live on the
    concrete local provider rather than on this port.
    """

    def authenticate(self, access_token: str) -> Principal:
        """Validate an access token and construct the caller's principal.

        Raises:
            AuthenticationError: the token is malformed, unsigned by a trusted key, expired, or
                otherwise not acceptable. The caller maps this onto a 401 (requirement 12.1); the
                message never distinguishes *why*, so a probe cannot learn whether a token was
                expired or forged.
        """
        ...


@runtime_checkable
class AuditSink(Protocol):
    """Audit write port (task 4.6, design §7.4).

    The one write path in an otherwise read-only platform, and deliberately narrow: a caller
    submits a completed :class:`~c360.security.audit.AuditRecord` and the sink is responsible for
    getting it durably written, append-only, off the request's latency path. Requirement 12.6
    makes the write mandatory for every customer read, agent invocation, unmask and export, and
    design §7.4 makes it fail *closed* — so ``submit`` reports back whether the record was accepted
    rather than silently dropping it.
    """

    def submit(self, record: AuditRecord) -> bool:
        """Enqueue ``record`` for durable, append-only write.

        Returns ``True`` when the record was accepted onto the queue and ``False`` when the queue
        is saturated — the fail-closed signal the API turns into a rejected request and a metric
        (design §7.4, §13.4).
        """
        ...


@runtime_checkable
class GraphRepository(Protocol):
    """Traversal over the projected relationship graph (design §5.3, task 5.3).

    Separate from :class:`RelationshipRepository` because it reads a different projection — the
    bidirectional ``graph_adjacency`` table (design §5.2), not the relational relationship tables —
    and answers a different question: reachable *structure* rather than a customer's stored links.
    Every method is bounded (hop cap, node cap) so a hub customer cannot turn a traversal into a
    full scan and blow the two-second budget (requirement 6.7).

    Node identity is the projection's typed composite id (``CUSTOMER:C00042``), not a bare customer
    id, because the graph spans sixteen node types and only some are customers. Redaction of
    non-entitled customer nodes is *not* done here: this port returns the true structure, and the
    service applies :func:`c360.security.graph_redaction.redact_view` before serialization so the UI
    and the Q&A agent share one filter (design §5.4).
    """

    def neighborhood(
        self,
        root: str,
        *,
        max_hops: int,
        node_cap: int,
        edge_types: frozenset[str] | None = None,
    ) -> GraphView:
        """The subgraph reachable from ``root`` within ``max_hops``, capped at ``node_cap``
        nodes."""
        ...

    def path_between(self, source: str, target: str, *, max_hops: int) -> tuple[str, ...]:
        """A shortest node-id path from ``source`` to ``target``, or empty if none within the
        cap."""
        ...

    def household_subgraph(self, household_id: str) -> GraphView:
        """The precomputed household subgraph (design §5.2)."""
        ...

    def degree_centrality(self, root: str) -> int:
        """The number of edges incident to ``root`` — its raw degree (design §5.3)."""
        ...


__all__ = [
    "AuditSink",
    "CustomerRepository",
    "FinancialRepository",
    "GraphRepository",
    "IdentityProvider",
    "JourneyRepository",
    "OfferRepository",
    "RelationshipRepository",
    "RiskRepository",
]
