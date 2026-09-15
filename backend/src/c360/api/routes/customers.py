"""Customer read endpoints (task 5.8, design §6.3).

Every route here follows one shape: authenticate (the middleware has already bound the principal),
authorize the target customer against the principal's entitlement (the 403-vs-404 gate, design
§7.2),
run the blocking service call off the event loop in a thread pool, and return the result through
:func:`masked_envelope` so the field-masking serializer runs on the response and no handler can skip
it. The one exception is ``GET /customers`` (search), which is scoped inside the query and returns
display-only hits that carry nothing maskable.

Response models are Pydantic wrappers over the service dataclasses. They exist so masking reaches
the
domain models nested inside them — :func:`c360.api.masking.mask_model` walks a ``BaseModel`` and
looks
up field rules by class name, so a risk profile inside a ``RiskResponse`` is masked exactly as it
would be if returned bare. Derived, non-sensitive fields (a risk band, an alert list, a rollup
total)
sit alongside the masked domain models and pass through unmasked, which is correct: they carry no
field a role is denied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from c360.api.auth import current_principal
from c360.api.envelope import ApiError, Envelope, ErrorCode
from c360.api.masking import masked_envelope
from c360.api.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from c360.api.services import Services, require_services
from c360.domain.enums import AccountType, EngagementChannel, EngagementEventType
from c360.domain.graph import GraphEdge, GraphNode
from c360.domain.models import (
    ContactInfo,
    CreditProfile,
    Customer,
    CustomerRelationship,
    CustomerSearchHit,
    EngagementEvent,
    FinancialProfile,
    Holding,
    Household,
    RiskProfile,
)
from c360.security.authorization import authorize_customer
from c360.security.entitlement import AllScope, BookScope
from c360.security.errors import EntitlementError
from c360.security.model import Principal
from c360.security.serializer import mask_model
from c360.services.aggregator import Customer360

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.services.financial import ExpenseAnalytics, MonthlyFlag
    from c360.services.journey import TimelineEntry
    from c360.services.offer import RankedOffer, RankedOffers
    from c360.services.relationship import HouseholdRollup
    from c360.services.risk import RiskAlert, RiskView

router = APIRouter(tags=["customers"])


# ================================================================ response models
class ProfileResponse(BaseModel):
    """Profile plus contact — the ``GET /customers/{id}`` payload (design §6.3)."""

    model_config = ConfigDict(frozen=True)

    profile: Customer
    contact: ContactInfo | None = None


class HoldingsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    holdings: tuple[Holding, ...] = ()


class FinancialResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    financial_profile: FinancialProfile | None = None
    credit_profile: CreditProfile | None = None


class CreditResponse(BaseModel):
    """The ``GET /customers/{id}/credit`` payload — the credit profile, masked per role.

    Kept separate from the financial profile so the FICO headline (requirement 5.10) can be fetched
    on its own by the dashboard's financial-overview widget without pulling the full holdings read.
    """

    model_config = ConfigDict(frozen=True)

    credit_profile: CreditProfile | None = None


class EngagementEventModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    event_type: str
    event_date: str
    channel: str
    session_id: str | None = None
    device_type: str | None = None
    outcome: str | None = None
    notes: str | None = None


class EngagementResponse(BaseModel):
    """The ``GET /customers/{id}/engagement`` payload — engagement events, newest first.

    Filterable by channel and event type (requirement 7.6). The events carry no per-role-masked
    field, so they render in full."""

    model_config = ConfigDict(frozen=True)

    events: tuple[EngagementEventModel, ...] = ()


class MonthlyFlagModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    month: str
    total_cents: int
    transaction_count: int
    is_anomaly: bool
    deviation_sigma: float | None = None


class ExpenseResponse(BaseModel):
    """Expense analytics. The category totals and monthly series carry no per-role-masked field,
    so they render in full; the deviation flags are derived, not stored, and are likewise open."""

    model_config = ConfigDict(frozen=True)

    by_category: tuple[dict[str, Any], ...] = ()
    monthly: tuple[MonthlyFlagModel, ...] = ()
    threshold_sigma: float


class RiskAlertModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    severity: int
    detail: str
    dismissible: bool


class RiskResponse(BaseModel):
    """Risk profile (masked per role) plus derived band, exposure and severity-ordered alerts."""

    model_config = ConfigDict(frozen=True)

    profile: RiskProfile
    band: str
    credit_exposure_cents: int
    alerts: tuple[RiskAlertModel, ...] = ()
    requires_compliance_indicator: bool


class GraphNodeModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_id: str
    node_type: str
    entity_id: str
    label: str
    props: dict[str, Any] = {}


class GraphEdgeModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    src_id: str
    dst_id: str
    edge_type: str
    is_inferred: bool = False
    confidence: float | None = None


class RelationshipsResponse(BaseModel):
    """Stored relationships plus the entitlement-redacted neighborhood subgraph (design §6.3).

    ``relationships`` are the relational links (masked where a role requires it); ``nodes`` and
    ``edges`` are the graph neighborhood after :func:`redact_view` has reduced any out-of-book
    customer node to structure-only. The two views answer different questions — the list is "who is
    this customer linked to", the graph is "what does the surrounding structure look like" — and
    both are on this endpoint because design §6.3 pairs them.
    """

    model_config = ConfigDict(frozen=True)

    relationships: tuple[CustomerRelationship, ...] = ()
    nodes: tuple[GraphNodeModel, ...] = ()
    edges: tuple[GraphEdgeModel, ...] = ()


class HouseholdResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    household: Household | None = None
    net_worth_cents: int | None = None
    total_deposits_cents: int | None = None
    product_count: int | None = None
    member_count: int | None = None


class OfferModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    expected_value_cents: int
    is_cross_sell: bool
    is_upsell: bool
    rationale: str
    suppressed: bool
    suppression_reason: str | None = None
    offer_id: str
    offer_name: str
    business_group: str
    offer_type: str


class OffersResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    offers: tuple[OfferModel, ...] = ()
    campaign_ids: tuple[str, ...] = ()


class TimelineEntryModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    entry_date: str
    title: str
    source_id: str
    amount_cents: int | None = None


class JourneyResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    timeline: tuple[TimelineEntryModel, ...] = ()


# ================================================================ helpers
def _authorize(principal: Principal, services: Services, customer_id: str) -> None:
    """Run the 403-vs-404 gate, then record the view for recently-viewed (requirement 3.6).

    :func:`authorize_customer` collapses "does not exist" and "not entitled" into one
    ``EntitlementError`` so a probe cannot use the status code as an existence oracle (requirement
    15.5). The API is nonetheless allowed to return 404 for a genuinely absent customer *that the
    caller would be entitled to* — because for such a caller there is no hidden row to protect. So a
    denial is re-mapped to 404 only when the caller's scope would have permitted the id had it
    existed (an ``ALL`` scope, or a ``BOOK`` that lists the id). A ``SEGMENT`` scope, or an id
    outside a book, stays a 403: there the distinction really would leak whether the row exists.
    """
    try:
        authorize_customer(principal, customer_id, services.customer.repository)
    except EntitlementError:
        if _scope_would_permit(principal.entitlement, customer_id):
            raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such customer") from None
        raise
    services.customer.record_view(principal.user_id, customer_id)


def _scope_would_permit(scope: object, customer_id: str) -> bool:
    """Whether ``scope`` would permit ``customer_id`` if it existed, without a query.

    ``ALL`` permits everything; a ``BOOK`` permits an id it lists. A ``SEGMENT`` scope cannot be
    decided without the (absent) customer's segment, so it is treated as "would not obviously
    permit" and the denial stays a 403 — the conservative choice that never turns the code into an
    oracle.
    """
    if isinstance(scope, AllScope):
        return True
    if isinstance(scope, BookScope):
        return customer_id in scope.customer_ids
    return False


def _require(value: object, customer_id: str) -> None:
    """Turn a ``None`` from an entitled read into a 404.

    Reached only *after* :func:`authorize_customer` has confirmed the customer is visible, so a
    ``None`` here means the sub-resource genuinely has no row (a customer with no risk profile), not
    an entitlement problem — and 404 is the honest answer for a resource that does not exist.
    """
    if value is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such customer resource")


# ================================================================ search
@router.get(
    "/customers",
    response_model=Envelope[Page[CustomerSearchHit]],
    summary="Search customers (entitlement-scoped, cursor-paginated)",
)
async def search_customers(
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> Envelope[Page[CustomerSearchHit]]:
    """FTS search over the customer index, scoped to the principal's book (requirements 3.1,
    3.3)."""
    page = await run_in_threadpool(
        services.customer.search, principal.entitlement, q, limit=limit, cursor=cursor
    )
    return Envelope.of(page)


# ================================================================ profile
@router.get(
    "/customers/{customer_id}",
    response_model=Envelope[dict[str, Any]],
    summary="Customer profile and contact",
)
async def get_customer(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    profile = await run_in_threadpool(services.customer.get_profile, customer_id)
    _require(profile, customer_id)
    assert profile is not None  # noqa: S101 - _require raised otherwise
    contact = await run_in_threadpool(services.customer.get_contact, customer_id)
    return masked_envelope(ProfileResponse(profile=profile, contact=contact), principal)


# ================================================================ holdings
@router.get(
    "/customers/{customer_id}/accounts",
    response_model=Envelope[dict[str, Any]],
    summary="All holdings (optionally filtered by account type)",
)
async def get_accounts(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    type_filter: Annotated[str | None, Query(alias="type")] = None,
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    account_types = _parse_account_types(type_filter)
    holdings = await run_in_threadpool(
        services.financial.get_holdings, customer_id, account_types=account_types
    )
    return masked_envelope(HoldingsResponse(holdings=tuple(holdings)), principal)


@router.get(
    "/customers/{customer_id}/loans",
    response_model=Envelope[dict[str, Any]],
    summary="Loan holdings",
)
async def get_loans(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    return await _holdings_of_type(customer_id, principal, services, account_type="LOAN")


@router.get(
    "/customers/{customer_id}/deposits",
    response_model=Envelope[dict[str, Any]],
    summary="Deposit holdings",
)
async def get_deposits(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    return await _holdings_of_type(customer_id, principal, services, account_type="DEPOSIT")


@router.get(
    "/customers/{customer_id}/investments",
    response_model=Envelope[dict[str, Any]],
    summary="Investment holdings and allocation",
)
async def get_investments(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    return await _holdings_of_type(customer_id, principal, services, account_type="INVESTMENT")


# ================================================================ transactions & expenses
@router.get(
    "/customers/{customer_id}/transactions",
    response_model=Envelope[dict[str, Any]],
    summary="Transactions with expense analytics",
)
async def get_transactions(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    analytics: ExpenseAnalytics = await run_in_threadpool(
        services.financial.get_expense_analytics, customer_id
    )
    return Envelope.of(_expense_payload(analytics))


# ================================================================ credit
@router.get(
    "/customers/{customer_id}/credit",
    response_model=Envelope[dict[str, Any]],
    summary="Credit profile (FICO, behaviour, utilization), masked per role",
)
async def get_credit(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    credit = await run_in_threadpool(services.financial.get_credit_profile, customer_id)
    return masked_envelope(CreditResponse(credit_profile=credit), principal)


# ================================================================ risk
@router.get(
    "/customers/{customer_id}/risk",
    response_model=Envelope[dict[str, Any]],
    summary="Risk profile, band, exposure and alerts",
)
async def get_risk(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    view: RiskView | None = await run_in_threadpool(services.risk.get_risk, customer_id)
    _require(view, customer_id)
    assert view is not None  # noqa: S101
    return masked_envelope(_risk_response(view), principal)


# ================================================================ relationships & household
@router.get(
    "/customers/{customer_id}/relationships",
    response_model=Envelope[dict[str, Any]],
    summary="Relationships",
)
async def get_relationships(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    relationships = await run_in_threadpool(services.relationship.get_relationships, customer_id)
    network = await run_in_threadpool(
        services.relationship.get_network, principal.entitlement, customer_id
    )
    response = RelationshipsResponse(
        relationships=tuple(relationships),
        nodes=tuple(_graph_node_model(node) for node in network.nodes),
        edges=tuple(_graph_edge_model(edge) for edge in network.edges),
    )
    return masked_envelope(response, principal)


@router.get(
    "/customers/{customer_id}/household",
    response_model=Envelope[dict[str, Any]],
    summary="Household and rollups",
)
async def get_household(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    household = await run_in_threadpool(services.relationship.get_household, customer_id)
    rollup: HouseholdRollup | None = await run_in_threadpool(
        services.relationship.get_household_rollup, customer_id
    )
    return masked_envelope(_household_response(household, rollup), principal)


# ================================================================ offers
@router.get(
    "/customers/{customer_id}/offers",
    response_model=Envelope[dict[str, Any]],
    summary="Ranked offers with suppression reasons and campaigns",
)
async def get_offers(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    ranked: RankedOffers = await run_in_threadpool(services.offer.get_offers, customer_id)
    return Envelope.of(_offers_payload(ranked))


# ================================================================ journey
@router.get(
    "/customers/{customer_id}/journey",
    response_model=Envelope[dict[str, Any]],
    summary="Merged customer timeline",
)
async def get_journey(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    timeline = await run_in_threadpool(services.journey.get_timeline, customer_id)
    return Envelope.of(_journey_payload(timeline))


# ================================================================ engagement
@router.get(
    "/customers/{customer_id}/engagement",
    response_model=Envelope[dict[str, Any]],
    summary="Engagement history, newest first, filterable by channel and event type",
)
async def get_engagement(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    channel: Annotated[str | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
) -> Envelope[dict[str, Any]]:
    """Engagement events across channels (requirement 7.6).

    The optional ``channel`` and ``event_type`` filters are comma-separated lists; an unknown value
    is a client error (422) rather than a silently-ignored filter, matching the ``type`` filter on
    holdings.
    """
    await run_in_threadpool(_authorize, principal, services, customer_id)
    channels = _parse_engagement_channels(channel)
    event_types = _parse_engagement_event_types(event_type)
    events = await run_in_threadpool(
        services.journey.get_engagement_history,
        customer_id,
        channels=channels,
        event_types=event_types,
    )
    return Envelope.of(_engagement_payload(events))


# ================================================================ 360
@router.get(
    "/customers/{customer_id}/360",
    response_model=Envelope[dict[str, Any]],
    summary="Full deterministic 360 read model (partial-tolerant)",
)
async def get_360(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    """The composed 360 view. A module that fails is isolated into ``meta.errors[]`` and its slot is
    omitted, so the response is a partial 200 rather than a whole-dashboard failure (requirement
    4.4).
    """
    await run_in_threadpool(_authorize, principal, services, customer_id)
    view = await run_in_threadpool(services.aggregator.build_360, principal, customer_id)
    if view.profile is None and not view.errors:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such customer")
    return _build_360_envelope(view, principal)


# ================================================================ conversion helpers
def _parse_account_types(raw: str | None) -> list[AccountType] | None:
    """Parse a comma-separated ``type`` filter into account types, or ``None`` for no filter.

    An unknown type is a client error (422), not silently ignored: dropping it would return every
    account for a request that asked for one type, which is a surprising and unsafe default.
    """
    if raw is None or not raw.strip():
        return None
    try:
        return [AccountType(part.strip().upper()) for part in raw.split(",") if part.strip()]
    except ValueError as error:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "unknown account type in filter") from error


def _parse_engagement_channels(raw: str | None) -> list[EngagementChannel] | None:
    """Parse a comma-separated ``channel`` filter into channels, or ``None`` for no filter."""
    if raw is None or not raw.strip():
        return None
    try:
        return [EngagementChannel(part.strip().upper()) for part in raw.split(",") if part.strip()]
    except ValueError as error:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "unknown engagement channel in filter"
        ) from error


def _parse_engagement_event_types(raw: str | None) -> list[EngagementEventType] | None:
    """Parse a comma-separated ``event_type`` filter into event types, or ``None`` for no filter."""
    if raw is None or not raw.strip():
        return None
    try:
        return [
            EngagementEventType(part.strip().upper()) for part in raw.split(",") if part.strip()
        ]
    except ValueError as error:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, "unknown engagement event type in filter"
        ) from error


def _engagement_payload(events: Sequence[EngagementEvent]) -> dict[str, Any]:
    return EngagementResponse(
        events=tuple(
            EngagementEventModel(
                event_id=event.event_id,
                event_type=event.event_type.value,
                event_date=event.event_date.isoformat(),
                channel=event.channel.value,
                session_id=event.session_id,
                device_type=event.device_type,
                outcome=event.outcome.value if event.outcome is not None else None,
                notes=event.notes,
            )
            for event in events
        )
    ).model_dump(mode="json")


async def _holdings_of_type(
    customer_id: str,
    principal: Principal,
    services: Services,
    *,
    account_type: str,
) -> Envelope[dict[str, Any]]:
    await run_in_threadpool(_authorize, principal, services, customer_id)
    holdings = await run_in_threadpool(
        services.financial.get_holdings,
        customer_id,
        account_types=[AccountType(account_type)],
    )
    return masked_envelope(HoldingsResponse(holdings=tuple(holdings)), principal)


def _monthly_model(flag: MonthlyFlag) -> MonthlyFlagModel:
    return MonthlyFlagModel(
        month=flag.month,
        total_cents=flag.total_cents,
        transaction_count=flag.transaction_count,
        is_anomaly=flag.is_anomaly,
        deviation_sigma=flag.deviation_sigma,
    )


def _expense_payload(analytics: ExpenseAnalytics) -> dict[str, Any]:
    return ExpenseResponse(
        by_category=tuple(total.model_dump(mode="json") for total in analytics.by_category),
        monthly=tuple(_monthly_model(flag) for flag in analytics.monthly),
        threshold_sigma=analytics.threshold_sigma,
    ).model_dump(mode="json")


def _risk_response(view: RiskView) -> RiskResponse:
    return RiskResponse(
        profile=view.profile,
        band=view.band.value,
        credit_exposure_cents=int(view.credit_exposure_cents),
        alerts=tuple(_alert_model(alert) for alert in view.alerts),
        requires_compliance_indicator=view.requires_compliance_indicator,
    )


def _alert_model(alert: RiskAlert) -> RiskAlertModel:
    return RiskAlertModel(
        category=alert.category,
        severity=int(alert.severity),
        detail=alert.detail,
        dismissible=alert.dismissible,
    )


def _household_response(
    household: Household | None, rollup: HouseholdRollup | None
) -> HouseholdResponse:
    if rollup is None:
        return HouseholdResponse(household=household)
    return HouseholdResponse(
        household=household,
        net_worth_cents=int(rollup.net_worth_cents),
        total_deposits_cents=int(rollup.total_deposits_cents),
        product_count=rollup.product_count,
        member_count=rollup.member_count,
    )


def _offer_model(ranked: RankedOffer) -> OfferModel:
    offer = ranked.offer.offer
    return OfferModel(
        rank=ranked.rank,
        expected_value_cents=ranked.expected_value_cents,
        is_cross_sell=ranked.is_cross_sell,
        is_upsell=ranked.is_upsell,
        rationale=ranked.rationale,
        suppressed=ranked.suppressed,
        suppression_reason=ranked.suppression_reason,
        offer_id=offer.offer_id,
        offer_name=offer.offer_name,
        business_group=offer.business_group.value,
        offer_type=offer.offer_type.value,
    )


def _offers_payload(ranked: RankedOffers) -> dict[str, Any]:
    return OffersResponse(
        offers=tuple(_offer_model(offer) for offer in ranked.offers),
        campaign_ids=tuple(campaign.campaign_id for campaign in ranked.campaigns),
    ).model_dump(mode="json")


def _graph_node_model(node: GraphNode) -> GraphNodeModel:
    return GraphNodeModel(
        node_id=node.node_id,
        node_type=node.node_type,
        entity_id=node.entity_id,
        label=node.label,
        props=node.props,
    )


def _graph_edge_model(edge: GraphEdge) -> GraphEdgeModel:
    return GraphEdgeModel(
        src_id=edge.src_id,
        dst_id=edge.dst_id,
        edge_type=edge.edge_type,
        is_inferred=edge.is_inferred,
        confidence=edge.confidence,
    )


def _timeline_model(entry: TimelineEntry) -> TimelineEntryModel:
    return TimelineEntryModel(
        category=entry.category.value,
        entry_date=entry.entry_date.isoformat(),
        title=entry.title,
        source_id=entry.source_id,
        amount_cents=entry.amount_cents,
    )


def _journey_payload(timeline: tuple[TimelineEntry, ...]) -> dict[str, Any]:
    return JourneyResponse(timeline=tuple(_timeline_model(entry) for entry in timeline)).model_dump(
        mode="json"
    )


def _build_360_envelope(view: Customer360, principal: Principal) -> Envelope[dict[str, Any]]:
    """Compose the 360 payload, masking each domain module and merging the per-module errors.

    Each present module is masked independently through :func:`mask_model` so the same field policy
    that governs the standalone endpoints governs the composed view; the union of masked-field paths
    is reported in ``meta.masked_fields`` with a module prefix so a client can attribute a
    redaction.
    Module failures ride ``meta.errors[]`` unchanged from the aggregator.
    """
    data: dict[str, Any] = {"customer_id": view.customer_id}
    masked_fields: list[str] = []

    def _add(name: str, model: BaseModel | None) -> None:
        if model is None:
            return
        payload, masked = mask_model(model, principal.field_policy)
        data[name] = payload
        masked_fields.extend(f"{name}.{path}" for path in masked)

    _add("profile", view.profile)
    _add("contact", view.contact)
    _add("household", view.household)
    _add("financial_profile", view.financial_profile)
    _add("risk", _risk_response(view.risk) if view.risk is not None else None)

    if view.expense_analytics is not None:
        data["expense_analytics"] = _expense_payload(view.expense_analytics)
    if view.offers is not None:
        data["offers"] = _offers_payload(view.offers)
    if view.timeline is not None:
        data["timeline"] = _journey_payload(view.timeline)
    if view.household_rollup is not None:
        rollup = view.household_rollup
        data["household_rollup"] = {
            "net_worth_cents": int(rollup.net_worth_cents),
            "total_deposits_cents": int(rollup.total_deposits_cents),
            "product_count": rollup.product_count,
            "member_count": rollup.member_count,
        }

    return Envelope.of(data, masked_fields=sorted(set(masked_fields)), errors=list(view.errors))


__all__ = ["router"]
