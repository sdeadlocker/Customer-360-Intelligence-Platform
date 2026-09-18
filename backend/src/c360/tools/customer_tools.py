"""The customer tools over the application services (task 6.2, requirements 11.2, 11.3, 11.6).

Sixteen tools, each calling the *same* service method the REST API calls, so a rule enforced in a
service or in the security layer is enforced identically on the tool path. The list mirrors the
design §8.2 tool set: profile, contact, holdings, financial_profile, credit_profile, risk_profile,
expense_analytics, transactions_query, offers, life_events, journey_timeline, household,
relationships, graph_neighborhood, graph_path and aggregate.

The masking discipline (requirement 11.6)
-----------------------------------------

A tool that returns customer fields runs the model through the same field-masking serializer the
REST boundary uses (:meth:`ToolContext.mask`) *before* turning anything into a fact. Facts are built
from the masked payload, never the raw model, so a value a role cannot see over REST cannot leak
into a fact a model would then narrate. Every read is preceded by :meth:`ToolContext.authorize`,
the same 403/404 gate the routes run, so a non-entitled customer id raises before any data is
touched (the agent turns that into a refusal — requirement 11.6). Graph reads pass the entitlement
scope to the relationship service, which already redacts non-entitled nodes to structure-only.

``aggregate`` (requirement 11.2)
--------------------------------

The one tool that is not a straight read. It computes a bounded set of whitelisted aggregates *in
SQL* through the existing repository aggregation paths — never by folding rows in Python — and
refuses any metric outside the whitelist, so a model cannot ask it to sum an arbitrary column.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from c360.domain.enums import (
    AccountStatus,
    AccountType,
    CustomerSegment,
    CustomerValue,
    EngagementChannel,
    EngagementEventType,
    RiskBand,
)
from c360.domain.models import (
    ContactInfo,
    CreditProfile,
    Customer,
    EngagementEvent,
    FinancialProfile,
    Holding,
    LifeEvent,
    RiskProfile,
    Transaction,
)
from c360.security.model import FieldGroup
from c360.tools.facts import FactTable, FactTableBuilder
from c360.tools.registry import ToolContext, ToolSpec

if TYPE_CHECKING:
    from c360.domain.graph import GraphEdge, GraphNode


# ================================================================ shared result shape


class ToolResult(BaseModel):
    """The common envelope every tool returns: a masked payload plus its fact table.

    ``data`` is the JSON-safe, role-masked payload — the same subset a REST caller of the equivalent
    endpoint would receive. ``facts`` is the provenance-bound view of the citable values in that
    payload (task 6.1). ``masked_fields`` mirrors ``meta.masked_fields`` so a caller can see what
    the role was not shown, exactly as over REST. ``entity_id`` is the customer the result is about.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str
    data: dict[str, Any] | list[Any]
    facts: FactTable = FactTable()
    masked_fields: tuple[str, ...] = ()


# ================================================================ argument models


class CustomerArgs(BaseModel):
    """Arguments for any tool keyed only on a customer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str = Field(min_length=1, description="The customer identifier to read.")


class CustomerSearchArgs(BaseModel):
    """Arguments for the cross-customer resolver tool (task 9.6, cross-customer Q&A).

    A single free-text query — a name, id, email, phone, account number, card last-4 or loan number
    — resolved against the FTS ``customer_search`` index, entitlement-scoped inside the query so a
    restricted book only ever surfaces its own customers. This is the tool the cross-customer Q&A
    agent calls first, to turn a question like "what are Jane Doe's risks?" into the ``customer_id``
    the reading tools then key on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Free-text search across name, customer id, email, phone, account number, "
            "card last-4 or loan number."
        ),
    )
    limit: int = Field(default=10, ge=1, le=25, description="Maximum matching customers to return.")


class CustomerCohortArgs(BaseModel):
    """Arguments for the book-level cohort tool (cross-customer Q&A, "high risk customers").

    Like :class:`CustomerSearchArgs` this is *not* keyed on a single customer: it describes a set.
    Every filter is optional, so "high risk customers", "my platinum clients" and "small-business
    customers past due" are all one tool with a different filter combination. The scope the query
    runs under is the principal's entitlement, applied in SQL — a caller cannot widen their book
    through this tool.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_band: RiskBand | None = Field(
        default=None,
        description="Restrict to exactly this risk band: LOW, MODERATE, ELEVATED or HIGH.",
    )
    min_risk_band: RiskBand | None = Field(
        default=None,
        description=(
            "Restrict to this risk band or higher, e.g. min_risk_band=ELEVATED returns the "
            "ELEVATED and HIGH customers. Use for 'high risk' / 'riskiest' cohort questions, "
            "which mean the top of the risk distribution rather than one exact band."
        ),
    )
    segments: tuple[CustomerSegment, ...] | None = Field(
        default=None, description="Restrict to these customer segments."
    )
    values: tuple[CustomerValue, ...] | None = Field(
        default=None, description="Restrict to these customer value tiers."
    )
    delinquent_only: bool = Field(
        default=False, description="Restrict to customers who are past due (not CURRENT)."
    )
    limit: int = Field(default=20, ge=1, le=100, description="Maximum customers to return.")


class PitchArgs(CustomerArgs):
    """Arguments for the pitch / talking-points tool for a single customer."""


class HoldingsArgs(CustomerArgs):
    """Holdings, optionally narrowed to specific account types."""

    account_types: tuple[AccountType, ...] | None = Field(
        default=None, description="Restrict to these account types, or all when omitted."
    )


class TransactionsArgs(CustomerArgs):
    """A bounded transaction query."""

    start_date: date | None = Field(default=None, description="Inclusive lower bound, ISO date.")
    end_date: date | None = Field(default=None, description="Inclusive upper bound, ISO date.")
    categories: tuple[str, ...] | None = Field(
        default=None, description="Restrict to these transaction categories."
    )
    limit: int = Field(default=200, ge=1, le=500, description="Maximum rows to return.")


class ExpenseAnalyticsArgs(CustomerArgs):
    """Expense analytics window."""

    start_date: date | None = None
    end_date: date | None = None
    months: int = Field(default=12, ge=1, le=36, description="Months of monthly trend to include.")


class LifeEventsArgs(CustomerArgs):
    """Life events for a customer."""


class EngagementArgs(CustomerArgs):
    """Engagement history, filterable by channel and event type (requirement 7.6)."""

    channels: tuple[EngagementChannel, ...] | None = None
    event_types: tuple[EngagementEventType, ...] | None = None
    limit: int = Field(default=100, ge=1, le=200)


class GraphPathArgs(BaseModel):
    """Two customers to find a relationship path between."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_customer_id: str = Field(min_length=1)
    target_customer_id: str = Field(min_length=1)


class AggregateMetric(StrEnum):
    """The whitelisted aggregates ``aggregate`` may compute (requirement 11.2).

    Deliberately small and closed: each maps to an existing SQL aggregation path. A metric outside
    this set is rejected by argument validation — the tool never computes an arbitrary sum.
    """

    OPEN_ACCOUNT_COUNT = "open_account_count"
    TOTAL_DEPOSITS_CENTS = "total_deposits_cents"
    TOTAL_LOANS_CENTS = "total_loans_cents"
    NET_WORTH_CENTS = "net_worth_cents"
    CREDIT_EXPOSURE_CENTS = "credit_exposure_cents"
    HOUSEHOLD_NET_WORTH_CENTS = "household_net_worth_cents"
    DEGREE_CENTRALITY = "degree_centrality"


class AggregateArgs(CustomerArgs):
    """A request for one whitelisted aggregate."""

    metric: AggregateMetric = Field(description="The whitelisted aggregate to compute in SQL.")


# ================================================================ response wrapper models
# These wrap domain models so the field-masking serializer, which looks rules up by the nested model
# class name, masks them exactly as it does under REST. They exist only to be masked; the tool then
# reads the masked payload back to build facts.


class _ProfileResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    profile: Customer
    contact: ContactInfo | None = None


class _ContactResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    contact: ContactInfo


class _HoldingsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    holdings: tuple[Holding, ...]


class _FinancialProfileResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    financial_profile: FinancialProfile


class _CreditProfileResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    credit_profile: CreditProfile


class _RiskProfileResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    risk_profile: RiskProfile
    band: str
    requires_compliance_indicator: bool


class _TransactionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    transactions: tuple[Transaction, ...]


class _LifeEventsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    life_events: tuple[LifeEvent, ...]


class _EngagementResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    events: tuple[EngagementEvent, ...]


# ================================================================ handlers


def _customer_search(context: ToolContext, args: CustomerSearchArgs) -> ToolResult:
    """Resolve a free-text query to entitled customers (cross-customer Q&A resolver).

    Unlike every other tool this one is *not* keyed on a customer id — it is how the cross-customer
    agent discovers which customer(s) a question is about. The search is entitlement-scoped inside
    the query (:meth:`CustomerService.search` passes ``principal.entitlement`` to the repository),
    so a restricted book only ever matches its own customers and the result count never reveals a
    customer outside it. The hits are the same display-only rows the search page shows (id, name,
    segment, city) — a picker, never citable financial data — so no facts are built here; the agent
    follows up with the reading tools (each of which re-authorizes the resolved id) to get figures.
    """
    page = context.services.customer.search(
        context.principal.entitlement, args.query, limit=args.limit
    )
    matches = [
        {
            "customer_id": hit.customer_id,
            "customer_name": hit.customer_name,
            "customer_segment": str(hit.customer_segment),
            "city": hit.city,
        }
        for hit in page.items
    ]
    return ToolResult(entity_id="", data={"matches": matches, "count": len(matches)})


def _customer_cohort(context: ToolContext, args: CustomerCohortArgs) -> ToolResult:
    """List the entitled book by a cohort criterion (cross-customer Q&A, "high risk customers").

    The book-level counterpart to :func:`_customer_search`: where search resolves a *named*
    customer, this answers a question about a *set* — a risk band, a segment, a value tier, or who
    is past due. Like search it is not keyed on a customer id and does not call
    :meth:`ToolContext.authorize`; instead it hands the principal's entitlement to
    :meth:`CustomerService.cohort`, which scopes the query in SQL so a restricted book only ever
    surfaces its own customers (requirement 3.3). The
    rows carry only coarse, non-maskable columns (id, name, segment, value tier, the derived risk
    band and delinquency status) — a picker into the reading tools, not citable financial data — so
    no facts are built here, exactly as for the resolver. The band label is the same LOW/MODERATE/
    ELEVATED/HIGH banding the risk module derives.
    """
    hits = context.services.customer.cohort(
        context.principal.entitlement,
        risk_band=args.risk_band,
        min_risk_band=args.min_risk_band,
        segments=args.segments,
        values=args.values,
        delinquent_only=args.delinquent_only,
        limit=args.limit,
    )
    members = [
        {
            "customer_id": hit.customer_id,
            "customer_name": hit.customer_name,
            "customer_segment": str(hit.customer_segment),
            "customer_value": str(hit.customer_value),
            "risk_band": hit.risk_band,
            "delinquency_status": (
                str(hit.delinquency_status) if hit.delinquency_status is not None else None
            ),
        }
        for hit in hits
    ]
    criterion = _cohort_criterion(args)
    return ToolResult(
        entity_id="",
        data={"members": members, "count": len(members), "criterion": criterion},
    )


def _cohort_criterion(args: CustomerCohortArgs) -> str:
    """A short, value-free label describing the cohort filter, for the answer's framing."""
    parts: list[str] = []
    if args.risk_band is not None:
        parts.append(f"{str(args.risk_band).lower()} risk")
    elif args.min_risk_band is not None:
        parts.append(f"{str(args.min_risk_band).lower()} risk or higher")
    if args.delinquent_only:
        parts.append("past due")
    if args.segments:
        parts.append("/".join(str(segment) for segment in args.segments))
    if args.values:
        parts.append("/".join(str(value) for value in args.values))
    return " ".join(parts) if parts else "all customers"


def _pitch(context: ToolContext, args: PitchArgs) -> ToolResult:
    """Compose a grounded set of talking points for a single customer (the "prepare a pitch" ask).

    Not a new read: it composes the *same* offer, financial and risk service calls the individual
    tools use, into one structured briefing a relationship manager can open a conversation from. The
    masking discipline is unchanged — every figure runs through :meth:`ToolContext.mask` before it
    becomes a fact, so a role that cannot see an expected offer value or a balance never gets it in
    a talking point. The recommended offers and their rationale, the headline financial position,
    and the severity-ordered risk alerts (compliance first, value-free labels) are the three
    sections; a live model narrates them into prose, the mock lists them, both citing the same
    facts.
    """
    context.authorize(args.customer_id)
    builder = FactTable.builder()
    masked_fields: list[str] = []

    offers_section, offer_masked = _pitch_offers(context, args.customer_id, builder)
    masked_fields.extend(offer_masked)
    financial_section, fin_masked = _pitch_financials(context, args.customer_id, builder)
    masked_fields.extend(fin_masked)
    risk_section = _pitch_risk(context, args.customer_id, builder)

    data: dict[str, Any] = {
        "offers": offers_section,
        "financial": financial_section,
        "risk": risk_section,
    }
    return ToolResult(
        entity_id=args.customer_id,
        data=data,
        facts=builder.build(),
        masked_fields=tuple(dict.fromkeys(masked_fields)),
    )


def _pitch_offers(
    context: ToolContext, customer_id: str, builder: FactTableBuilder
) -> tuple[dict[str, Any], list[str]]:
    """The pitch's offer section: the ranked live offers, with expected value cited only if visible.

    Mirrors :func:`_offers`' masking discipline exactly — the expected value is an OFFERS-group
    maskable field, so each offer's :class:`CustomerOffer` is masked and the value is cited only
    when the serializer kept it for this role.
    """
    ranked = context.services.offer.get_offers(customer_id)
    live = [item for item in ranked.offers if not item.suppressed]
    offers = [
        {
            "offer_id": item.offer.offer.offer_id,
            "offer_name": item.offer.offer.offer_name,
            "rank": item.rank,
            "is_cross_sell": item.is_cross_sell,
            "is_upsell": item.is_upsell,
            "rationale": item.rationale,
        }
        for item in live
    ]
    masked_fields: list[str] = []
    for item in live:
        offer_data, paths = context.mask(item.offer.customer_offer)
        masked_fields.extend(paths)
        if isinstance(offer_data, dict) and "expected_value_cents" in offer_data:
            builder.add(
                entity_type="offer",
                entity_id=item.offer.offer.offer_id,
                field="expected_value_cents",
                value=offer_data["expected_value_cents"],
                as_of=item.offer.as_of_date.isoformat(),
                source_system=item.offer.source_system,
            )
    return {"recommended": offers}, masked_fields


def _pitch_financials(
    context: ToolContext, customer_id: str, builder: FactTableBuilder
) -> tuple[dict[str, Any], list[str]]:
    """The pitch's financial headline, masked exactly as :func:`_financial_profile` does."""
    profile = context.services.financial.get_financial_profile(customer_id)
    if profile is None:
        return {}, []
    data, masked_fields = context.mask(_FinancialProfileResponse(financial_profile=profile))
    _facts_from_masked(
        builder,
        _nested(data, "financial_profile"),
        entity_type="financial_profile",
        entity_id=customer_id,
        fields=(
            "net_worth_cents",
            "total_deposits_cents",
            "total_loans_cents",
            "monthly_income_cents",
        ),
        as_of=profile.as_of_date.isoformat(),
        source_system=profile.source_system,
    )
    return _nested(data, "financial_profile") or {}, list(masked_fields)


def _pitch_risk(
    context: ToolContext, customer_id: str, builder: FactTableBuilder
) -> dict[str, Any]:
    """The pitch's risk section: band, compliance flag and the value-free, severity-ordered alerts.

    The band and the compliance flag are non-maskable and always citable, exactly as in
    :func:`_risk_profile`; the alert details are already value-free labels the risk service
    produced, safe to surface in a talking point.
    """
    view = context.services.risk.get_risk(customer_id)
    if view is None:
        return {}
    builder.add(
        entity_type="risk_profile",
        entity_id=customer_id,
        field="band",
        value=str(view.band),
        as_of=view.profile.as_of_date.isoformat(),
        source_system=view.profile.source_system,
    )
    alerts = [
        {"category": alert.category, "detail": alert.detail, "dismissible": alert.dismissible}
        for alert in view.alerts
    ]
    return {
        "band": str(view.band),
        "requires_compliance_indicator": view.requires_compliance_indicator,
        "alerts": alerts,
    }


def _profile(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    profile = context.services.customer.get_profile(args.customer_id)
    if profile is None:
        return ToolResult(entity_id=args.customer_id, data={})
    contact = context.services.customer.get_contact(args.customer_id)
    data, masked_fields = context.mask(_ProfileResponse(profile=profile, contact=contact))
    builder = FactTable.builder()
    _facts_from_masked(
        builder,
        _nested(data, "profile"),
        entity_type="customer",
        entity_id=args.customer_id,
        fields=("customer_name", "customer_segment", "customer_value", "customer_since"),
        as_of=profile.as_of_date.isoformat(),
        source_system=profile.source_system,
    )
    return ToolResult(
        entity_id=args.customer_id,
        data=data,
        facts=builder.build(),
        masked_fields=tuple(masked_fields),
    )


def _contact(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    contact = context.services.customer.get_contact(args.customer_id)
    if contact is None:
        return ToolResult(entity_id=args.customer_id, data={})
    data, masked_fields = context.mask(_ContactResponse(contact=contact))
    return ToolResult(entity_id=args.customer_id, data=data, masked_fields=tuple(masked_fields))


def _holdings(context: ToolContext, args: HoldingsArgs) -> ToolResult:
    context.authorize(args.customer_id)
    holdings = context.services.financial.get_holdings(
        args.customer_id, account_types=args.account_types
    )
    data, masked_fields = context.mask(_HoldingsResponse(holdings=tuple(holdings)))
    builder = FactTable.builder()
    for index, holding in enumerate(holdings):
        account = holding.account
        masked_account = _nested(data, "holdings", index, "account")
        if masked_account is not None and "balance_cents" in masked_account:
            builder.add(
                entity_type="account",
                entity_id=account.account_id,
                field="balance_cents",
                value=masked_account["balance_cents"],
                as_of=account.as_of_date.isoformat(),
                source_system=account.source_system,
            )
    return ToolResult(
        entity_id=args.customer_id,
        data=data,
        facts=builder.build(),
        masked_fields=tuple(masked_fields),
    )


def _financial_profile(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    profile = context.services.financial.get_financial_profile(args.customer_id)
    if profile is None:
        return ToolResult(entity_id=args.customer_id, data={})
    data, masked_fields = context.mask(_FinancialProfileResponse(financial_profile=profile))
    builder = FactTable.builder()
    _facts_from_masked(
        builder,
        _nested(data, "financial_profile"),
        entity_type="financial_profile",
        entity_id=args.customer_id,
        fields=(
            "net_worth_cents",
            "total_deposits_cents",
            "total_loans_cents",
            "total_investments_cents",
            "monthly_income_cents",
            "monthly_expense_cents",
        ),
        as_of=profile.as_of_date.isoformat(),
        source_system=profile.source_system,
    )
    return ToolResult(
        entity_id=args.customer_id,
        data=data,
        facts=builder.build(),
        masked_fields=tuple(masked_fields),
    )


def _credit_profile(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    profile = context.services.financial.get_credit_profile(args.customer_id)
    if profile is None:
        return ToolResult(entity_id=args.customer_id, data={})
    data, masked_fields = context.mask(_CreditProfileResponse(credit_profile=profile))
    builder = FactTable.builder()
    _facts_from_masked(
        builder,
        _nested(data, "credit_profile"),
        entity_type="credit_profile",
        entity_id=args.customer_id,
        fields=("fico_score", "credit_exposure_cents"),
        as_of=profile.as_of_date.isoformat(),
        source_system=profile.source_system,
    )
    return ToolResult(
        entity_id=args.customer_id,
        data=data,
        facts=builder.build(),
        masked_fields=tuple(masked_fields),
    )


def _risk_profile(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    view = context.services.risk.get_risk(args.customer_id)
    if view is None:
        return ToolResult(entity_id=args.customer_id, data={})
    response = _RiskProfileResponse(
        risk_profile=view.profile,
        band=str(view.band),
        requires_compliance_indicator=view.requires_compliance_indicator,
    )
    data, masked_fields = context.mask(response)
    builder = FactTable.builder()
    _facts_from_masked(
        builder,
        _nested(data, "risk_profile"),
        entity_type="risk_profile",
        entity_id=args.customer_id,
        fields=("risk_score", "delinquency_status"),
        as_of=view.profile.as_of_date.isoformat(),
        source_system=view.profile.source_system,
    )
    # The band and the compliance flag are not maskable and are always citable.
    builder.add(
        entity_type="risk_profile",
        entity_id=args.customer_id,
        field="band",
        value=str(view.band),
        as_of=view.profile.as_of_date.isoformat(),
        source_system=view.profile.source_system,
    )
    return ToolResult(
        entity_id=args.customer_id,
        data=data,
        facts=builder.build(),
        masked_fields=tuple(masked_fields),
    )


def _expense_analytics(context: ToolContext, args: ExpenseAnalyticsArgs) -> ToolResult:
    context.authorize(args.customer_id)
    analytics = context.services.financial.get_expense_analytics(
        args.customer_id,
        start_date=args.start_date,
        end_date=args.end_date,
        months=args.months,
    )
    # Expense analytics carry no maskable fields (aggregates, not raw account values), so the
    # payload is built directly and every total is a citable fact.
    builder = FactTable.builder()
    by_category = [
        {
            "transaction_category": row.transaction_category,
            "total_cents": int(row.total_cents),
            "transaction_count": row.transaction_count,
        }
        for row in analytics.by_category
    ]
    for row in analytics.by_category:
        builder.add(
            entity_type="expense_category",
            entity_id=f"{args.customer_id}:{row.transaction_category}",
            field="total_cents",
            value=int(row.total_cents),
        )
    monthly = [
        {
            "month": flag.month,
            "total_cents": flag.total_cents,
            "transaction_count": flag.transaction_count,
            "is_anomaly": flag.is_anomaly,
            "deviation_sigma": flag.deviation_sigma,
        }
        for flag in analytics.monthly
    ]
    data = {
        "by_category": by_category,
        "monthly": monthly,
        "threshold_sigma": analytics.threshold_sigma,
    }
    return ToolResult(entity_id=args.customer_id, data=data, facts=builder.build())


def _transactions_query(context: ToolContext, args: TransactionsArgs) -> ToolResult:
    context.authorize(args.customer_id)
    transactions = context.services.financial.get_transactions(
        args.customer_id,
        start_date=args.start_date,
        end_date=args.end_date,
        categories=args.categories,
        limit=args.limit,
    )
    data, masked_fields = context.mask(_TransactionsResponse(transactions=tuple(transactions)))
    return ToolResult(entity_id=args.customer_id, data=data, masked_fields=tuple(masked_fields))


def _offers(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    ranked = context.services.offer.get_offers(args.customer_id)
    offers = [
        {
            "offer_id": item.offer.offer.offer_id,
            "rank": item.rank,
            "is_cross_sell": item.is_cross_sell,
            "is_upsell": item.is_upsell,
            "rationale": item.rationale,
            "suppressed": item.suppressed,
            "suppression_reason": item.suppression_reason,
        }
        for item in ranked.offers
    ]
    campaigns = [
        {"campaign_id": campaign.campaign_id, "campaign_name": campaign.campaign_name}
        for campaign in ranked.campaigns
    ]
    # Expected value is an OFFERS-group field; only cite it when the serializer would show it to
    # this role. Rather than re-decide that, mask the CustomerOffer per offer and read it back.
    builder = FactTable.builder()
    for item in ranked.offers:
        offer_data, _paths = context.mask(item.offer.customer_offer)
        if isinstance(offer_data, dict) and "expected_value_cents" in offer_data:
            builder.add(
                entity_type="offer",
                entity_id=item.offer.offer.offer_id,
                field="expected_value_cents",
                value=offer_data["expected_value_cents"],
                as_of=item.offer.as_of_date.isoformat(),
                source_system=item.offer.source_system,
            )
    return ToolResult(
        entity_id=args.customer_id,
        data={"offers": offers, "campaigns": campaigns},
        facts=builder.build(),
    )


def _life_events(context: ToolContext, args: LifeEventsArgs) -> ToolResult:
    context.authorize(args.customer_id)
    life_events = context.services.journey.get_life_events(args.customer_id)
    data, masked_fields = context.mask(_LifeEventsResponse(life_events=tuple(life_events)))
    return ToolResult(entity_id=args.customer_id, data=data, masked_fields=tuple(masked_fields))


def _journey_timeline(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    timeline = context.services.journey.get_timeline(args.customer_id)
    entries = [
        {
            "category": str(entry.category),
            "entry_date": entry.entry_date.isoformat(),
            "title": entry.title,
            "source_id": entry.source_id,
            "amount_cents": entry.amount_cents,
        }
        for entry in timeline
    ]
    return ToolResult(entity_id=args.customer_id, data={"timeline": entries})


def _household(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    rollup = context.services.relationship.get_household_rollup(args.customer_id)
    if rollup is None:
        return ToolResult(entity_id=args.customer_id, data={})
    # Rollup values are BALANCES-group aggregates; ask the role's field policy whether that group is
    # visible, reusing the same matrix the serializer uses rather than re-deciding.
    builder = FactTable.builder()
    visible = context.principal.field_policy.is_visible(FieldGroup.BALANCES)
    data: dict[str, Any] = {
        "household_id": rollup.household_id,
        "member_count": rollup.member_count,
        "product_count": rollup.product_count,
    }
    if visible:
        data["net_worth_cents"] = int(rollup.net_worth_cents)
        data["total_deposits_cents"] = int(rollup.total_deposits_cents)
        builder.add(
            entity_type="household",
            entity_id=rollup.household_id,
            field="net_worth_cents",
            value=int(rollup.net_worth_cents),
        )
    return ToolResult(entity_id=args.customer_id, data=data, facts=builder.build())


def _relationships(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    relationships = context.services.relationship.get_relationships(args.customer_id)
    rels = [
        {
            "relationship_id": rel.relationship_id,
            "relationship_type": str(rel.relationship_type),
            "counterparty": rel.counterparty_of(args.customer_id),
            "is_inferred": rel.is_inferred,
            "confidence": rel.confidence,
        }
        for rel in relationships
    ]
    return ToolResult(entity_id=args.customer_id, data={"relationships": rels})


def _graph_neighborhood(context: ToolContext, args: CustomerArgs) -> ToolResult:
    context.authorize(args.customer_id)
    view = context.services.relationship.get_network(
        context.principal.entitlement, args.customer_id
    )
    return ToolResult(
        entity_id=args.customer_id,
        data={
            "nodes": [_node_dict(node) for node in view.nodes],
            "edges": [_edge_dict(edge) for edge in view.edges],
        },
    )


def _graph_path(context: ToolContext, args: GraphPathArgs) -> ToolResult:
    context.authorize(args.source_customer_id)
    context.authorize(args.target_customer_id)
    path = context.services.relationship.path_between(
        args.source_customer_id, args.target_customer_id
    )
    return ToolResult(
        entity_id=args.source_customer_id,
        data={"path": list(path), "length": max(0, len(path) - 1)},
    )


def _aggregate(context: ToolContext, args: AggregateArgs) -> ToolResult:
    """Compute one whitelisted aggregate in SQL (requirement 11.2)."""
    context.authorize(args.customer_id)
    metric = args.metric
    builder = FactTable.builder()
    value = _compute_aggregate(context, args.customer_id, metric)
    if value is not None:
        builder.add(
            entity_type="aggregate",
            entity_id=args.customer_id,
            field=str(metric),
            value=value,
        )
    return ToolResult(
        entity_id=args.customer_id,
        data={"metric": str(metric), "value": value},
        facts=builder.build(),
    )


# ================================================================ helpers


def _compute_aggregate(
    context: ToolContext, customer_id: str, metric: AggregateMetric
) -> int | None:
    """Dispatch one whitelisted metric to its SQL-backed service path.

    A dispatch table rather than a chain of returns: every metric resolves to a single callable, so
    the whitelist and its computation stay side by side and adding a metric is one table entry.
    """
    computed = _AGGREGATE_DISPATCH[metric]
    return computed(context, customer_id)


def _aggregate_open_account_count(context: ToolContext, customer_id: str) -> int:
    return len(
        context.services.financial.get_holdings(customer_id, statuses=[AccountStatus.ACTIVE])
    )


def _aggregate_household_net_worth(context: ToolContext, customer_id: str) -> int | None:
    rollup = context.services.relationship.get_household_rollup(customer_id)
    return None if rollup is None else int(rollup.net_worth_cents)


def _aggregate_profile_field(field: str) -> Callable[[ToolContext, str], int | None]:
    def compute(context: ToolContext, customer_id: str) -> int | None:
        profile = context.services.financial.get_financial_profile(customer_id)
        return None if profile is None else int(getattr(profile, field))

    return compute


#: Each whitelisted metric to the callable that computes it from a service (requirement 11.2).
_AGGREGATE_DISPATCH: dict[AggregateMetric, Callable[[ToolContext, str], int | None]] = {
    AggregateMetric.OPEN_ACCOUNT_COUNT: _aggregate_open_account_count,
    AggregateMetric.DEGREE_CENTRALITY: (
        lambda context, customer_id: context.services.relationship.degree_centrality(customer_id)
    ),
    AggregateMetric.CREDIT_EXPOSURE_CENTS: (
        lambda context, customer_id: int(context.services.risk.get_exposure_cents(customer_id))
    ),
    AggregateMetric.HOUSEHOLD_NET_WORTH_CENTS: _aggregate_household_net_worth,
    AggregateMetric.TOTAL_DEPOSITS_CENTS: _aggregate_profile_field("total_deposits_cents"),
    AggregateMetric.TOTAL_LOANS_CENTS: _aggregate_profile_field("total_loans_cents"),
    AggregateMetric.NET_WORTH_CENTS: _aggregate_profile_field("net_worth_cents"),
}


def _facts_from_masked(
    builder: FactTableBuilder,
    masked_model: dict[str, Any] | None,
    *,
    entity_type: str,
    entity_id: str,
    fields: tuple[str, ...],
    as_of: str | None,
    source_system: str | None,
) -> None:
    """Add a fact for each field the serializer *kept*, using the masked value.

    A field the role could not see was dropped from the masked payload (HIDDEN) or replaced by its
    band/partial form; either way the value that becomes a fact is the value the role is allowed to
    see, so a fact never carries a figure the same role could not read over REST.
    """
    if masked_model is None:
        return
    for field in fields:
        if field in masked_model:
            builder.add(
                entity_type=entity_type,
                entity_id=entity_id,
                field=field,
                value=masked_model[field],
                as_of=as_of,
                source_system=source_system,
            )


def _nested(payload: dict[str, Any] | list[Any], *keys: Any) -> dict[str, Any] | None:
    """Walk a masked payload by keys/indices, returning a dict or ``None`` if the path is absent."""
    node: Any = payload
    for key in keys:
        is_dict_hit = isinstance(node, dict) and isinstance(key, str) and key in node
        is_list_hit = isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node)
        if is_dict_hit or is_list_hit:
            node = node[key]
        else:
            return None
    return node if isinstance(node, dict) else None


def _node_dict(node: GraphNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "node_type": node.node_type,
        "entity_id": node.entity_id,
        "label": node.label,
        "props": dict(node.props),
    }


def _edge_dict(edge: GraphEdge) -> dict[str, Any]:
    return {
        "src_id": edge.src_id,
        "dst_id": edge.dst_id,
        "edge_type": edge.edge_type,
        "is_inferred": edge.is_inferred,
        "confidence": edge.confidence,
    }


# ================================================================ registry export


def _spec(
    name: str,
    description: str,
    args_model: type[BaseModel],
    handler: Any,
) -> ToolSpec[Any, ToolResult]:
    return ToolSpec(
        name=name,
        description=description,
        args_model=args_model,
        result_model=ToolResult,
        handler=handler,
    )


#: Every customer tool, registered by :func:`c360.tools.registry.build_tool_registry`.
CUSTOMER_TOOL_SPECS: tuple[ToolSpec[Any, ToolResult], ...] = (
    _spec(
        "customer_search",
        (
            "Find customers by name, id, email, phone, account number, card last-4 or loan number. "
            "Use this first to resolve which customer a question is about before reading their "
            "details. Returns display-only matches (id, name, segment, city), entitlement-scoped."
        ),
        CustomerSearchArgs,
        _customer_search,
    ),
    _spec(
        "customer_cohort",
        (
            "List customers across the entitled book by a cohort criterion. ALWAYS use this tool "
            "for any question about a *group* of customers ('high risk customers', 'my platinum "
            "clients', 'who is past due') rather than one named customer — never answer such a "
            "question from memory. For 'high risk' or 'the riskiest' customers, pass "
            "min_risk_band=ELEVATED (the band and above) rather than risk_band=HIGH, because 'high "
            "risk' colloquially means the top of the distribution and an exact top band may be "
            "empty. Use risk_band only when the user names an exact band (e.g. 'moderate risk "
            "customers'). Also filters by segment, value tier and delinquency (delinquent_only). "
            "When NO filter is given the results are ranked by customer value (highest-value "
            "first), so use this tool with no filters for 'who is my most valuable / highest-value "
            "customer', 'which customer can give the most profit', or 'who should I pitch / who "
            "has the best opportunity' — it returns the book ranked by value. Returns display-only "
            "ranked members (id, name, segment, value, risk band, delinquency), entitlement-scoped."
        ),
        CustomerCohortArgs,
        _customer_cohort,
    ),
    _spec("profile", "Customer profile and headline attributes.", CustomerArgs, _profile),
    _spec("contact", "Customer contact information (masked per role).", CustomerArgs, _contact),
    _spec("holdings", "All accounts and product holdings.", HoldingsArgs, _holdings),
    _spec(
        "financial_profile",
        "Balances, net worth, income and expense headline values.",
        CustomerArgs,
        _financial_profile,
    ),
    _spec("credit_profile", "Credit scores and exposure.", CustomerArgs, _credit_profile),
    _spec(
        "risk_profile",
        "Risk scores, band and delinquency indicators.",
        CustomerArgs,
        _risk_profile,
    ),
    _spec(
        "expense_analytics",
        "Category totals and monthly spend trend with deviation flags.",
        ExpenseAnalyticsArgs,
        _expense_analytics,
    ),
    _spec(
        "transactions_query",
        "Bounded transaction query by date range and category.",
        TransactionsArgs,
        _transactions_query,
    ),
    _spec("offers", "Ranked offers with rationale and suppression status.", CustomerArgs, _offers),
    _spec(
        "pitch",
        (
            "ALWAYS call this tool when asked to prepare a pitch, prepare talking points, or 'what "
            "should I tell / what do I say to' a customer — never compose such a briefing from "
            "memory. It returns the grounded material to build the pitch from: the recommended "
            "offers with rationale, the headline financial position, and the risk and compliance "
            "flags, composed from the offer, financial and risk reads, masked per role and cited "
            "to the record. Narrate the returned sections into talking points; cite every figure "
            "by its [F] id."
        ),
        PitchArgs,
        _pitch,
    ),
    _spec("life_events", "Detected and recorded life events.", LifeEventsArgs, _life_events),
    _spec("journey_timeline", "Merged customer timeline.", CustomerArgs, _journey_timeline),
    _spec("household", "Household membership and rollups.", CustomerArgs, _household),
    _spec(
        "relationships",
        "Customer relationships and counterparties.",
        CustomerArgs,
        _relationships,
    ),
    _spec(
        "graph_neighborhood",
        "The customer's relationship neighborhood, entitlement-redacted.",
        CustomerArgs,
        _graph_neighborhood,
    ),
    _spec(
        "graph_path",
        "A relationship path between two customers.",
        GraphPathArgs,
        _graph_path,
    ),
    _spec(
        "aggregate",
        "Compute one whitelisted aggregate in SQL.",
        AggregateArgs,
        _aggregate,
    ),
)


__all__ = ["CUSTOMER_TOOL_SPECS", "AggregateMetric", "ToolResult"]
