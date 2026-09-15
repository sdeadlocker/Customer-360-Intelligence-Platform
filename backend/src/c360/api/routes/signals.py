"""The proactive-alerting worklist endpoints (task 17.5).

Four routes turn the detected, ranked signals into a usable feed:

* ``GET /signals`` — the caller's cross-book ranked queue, entitlement-scoped, cursor-paginated,
  filterable by type and severity;
* ``GET /customers/{id}/signals`` — the signals for one customer, behind the same 403-vs-404 gate
  the 360 view uses;
* ``POST /signals/{id}/dismiss`` and ``POST /signals/{id}/ack`` — the per-user state writes, each
  re-checking that the caller is entitled to the customer the signal is about before writing.

Every payload passes through :func:`masked_envelope`, so a signal's ``value_at_stake_cents`` is
masked exactly as any balance is (it is registered in the field map under the BALANCES group) — a
role that cannot see balances sees a banded value, never the figure. The feed carries no other
maskable value: evidence summaries and detail chips are value-free labels by construction (task
17.2).

When ``signals.db`` has not been built yet (``c360 detect-signals`` has not run), the worklist is
simply empty rather than an error, mirroring the not-yet-ingested knowledge story.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from c360.api.auth import current_principal
from c360.api.envelope import ApiError, Envelope, ErrorCode
from c360.api.masking import masked_envelope
from c360.api.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page
from c360.api.services import Services, require_services
from c360.security.authorization import authorize_customer
from c360.security.entitlement import AllScope, BookScope
from c360.security.errors import EntitlementError
from c360.security.model import Principal
from c360.signals.models import Citation, Evidence, Severity, Signal, SignalType

router = APIRouter(tags=["signals"])


# ================================================================ response models
class CitationModel(BaseModel):
    """A fact citation backing a signal (requirement 10.8)."""

    model_config = ConfigDict(frozen=True)

    entity_type: str
    entity_id: str
    field: str
    as_of: str


class EvidenceModel(BaseModel):
    """The value-free "why" behind a signal — summary, citations and label chips."""

    model_config = ConfigDict(frozen=True)

    summary: str
    citations: tuple[CitationModel, ...] = ()
    details: dict[str, str] = Field(default_factory=dict)


class SignalModel(BaseModel):
    """One signal as the API returns it (task 17.5).

    ``value_at_stake_cents`` is the one maskable field: it is registered under the BALANCES group in
    the field map, so :func:`masked_envelope` bands it for a role without balance entitlement.
    Everything else is a low-sensitivity label or an id.
    """

    model_config = ConfigDict(frozen=True)

    signal_id: int
    customer_id: str
    signal_type: SignalType
    severity: str
    score: float
    value_at_stake_cents: int
    evidence: EvidenceModel
    as_of: str
    detected_at: str
    status: str


class DismissResult(BaseModel):
    """The outcome of a dismiss/ack write."""

    model_config = ConfigDict(frozen=True)

    signal_id: int
    status: str


def _citation_model(citation: Citation) -> CitationModel:
    return CitationModel(
        entity_type=citation.entity_type,
        entity_id=citation.entity_id,
        field=citation.field,
        as_of=citation.as_of,
    )


def _evidence_model(evidence: Evidence) -> EvidenceModel:
    return EvidenceModel(
        summary=evidence.summary,
        citations=tuple(_citation_model(c) for c in evidence.citations),
        details=dict(evidence.details),
    )


def _signal_model(signal: Signal) -> SignalModel:
    return SignalModel(
        signal_id=signal.signal_id,
        customer_id=signal.customer_id,
        signal_type=signal.signal_type,
        severity=signal.severity.name,
        score=round(signal.score, 6),
        value_at_stake_cents=signal.value_at_stake_cents,
        evidence=_evidence_model(signal.evidence),
        as_of=signal.as_of.isoformat(),
        detected_at=signal.detected_at,
        status=str(signal.status),
    )


def _parse_types(raw: str | None) -> tuple[SignalType, ...] | None:
    """Parse a comma-separated ``type`` filter into signal types, ignoring unknown tokens."""
    if not raw:
        return None
    parsed: list[SignalType] = []
    for token in raw.split(","):
        candidate = token.strip().upper()
        if candidate in SignalType.__members__.values() or candidate in {
            t.value for t in SignalType
        }:
            parsed.append(SignalType(candidate))
    return tuple(parsed) or None


def _parse_min_severity(raw: str | None) -> Severity | None:
    """Parse a minimum-severity filter (INFO/WARNING/CRITICAL), or ``None`` if unset/unknown."""
    if not raw:
        return None
    name = raw.strip().upper()
    return Severity[name] if name in Severity.__members__ else None


# ================================================================ cross-book worklist
@router.get(
    "/signals",
    response_model=Envelope[Page[dict[str, Any]]],
    summary="The caller's ranked, entitlement-scoped signals worklist",
)
async def list_signals(
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
    type_filter: Annotated[str | None, Query(alias="type")] = None,
    min_severity: Annotated[str | None, Query(alias="severity")] = None,
) -> Envelope[Page[dict[str, Any]]]:
    """The prioritized daily worklist across the caller's entitled book (task 17.3, 17.5).

    Ranked by severity x value-at-stake x recency, suppressing dismissed and cooled-off signals.
    Returns an empty page when signal detection has not run yet.
    """
    signals = services.signals
    if signals is None:
        return Envelope.of(Page(items=[], next_cursor=None))

    page = await run_in_threadpool(
        signals.worklist,
        principal.entitlement,
        principal.user_id,
        limit=limit,
        cursor=cursor,
        signal_types=_parse_types(type_filter),
        min_severity=_parse_min_severity(min_severity),
    )
    return _masked_page(page.items, page.next_cursor, principal)


@router.get(
    "/customers/{customer_id}/signals",
    response_model=Envelope[dict[str, Any]],
    summary="Signals for one customer (drill-down)",
)
async def list_customer_signals(
    customer_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    """Every signal for one customer, behind the same 403-vs-404 gate as the 360 view (task
    17.5)."""
    await run_in_threadpool(_authorize_customer, principal, services, customer_id)
    signals = services.signals
    items: tuple[Signal, ...] = ()
    if signals is not None:
        items = await run_in_threadpool(signals.for_customer, customer_id, principal.user_id)
    return masked_envelope(_SignalList(signals=tuple(_signal_model(s) for s in items)), principal)


# ================================================================ state writes
@router.post(
    "/signals/{signal_id}/dismiss",
    response_model=Envelope[DismissResult],
    summary="Dismiss a signal for the calling user",
    responses={404: {"description": "No such signal, or not entitled to its customer"}},
)
async def dismiss_signal(
    signal_id: int,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[DismissResult]:
    """Suppress a signal from the caller's queue for the cooling-off window (task 17.5)."""
    return await _set_state(signal_id, principal, services, action="dismiss")


@router.post(
    "/signals/{signal_id}/ack",
    response_model=Envelope[DismissResult],
    summary="Acknowledge (mark actioned) a signal for the calling user",
    responses={404: {"description": "No such signal, or not entitled to its customer"}},
)
async def ack_signal(
    signal_id: int,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[DismissResult]:
    """Mark a signal actioned for the caller, permanently suppressing it from their queue (task
    17.5)."""
    return await _set_state(signal_id, principal, services, action="ack")


# ================================================================ helpers
class _SignalList(BaseModel):
    """Wrapper so :func:`masked_envelope` walks the nested signal models."""

    model_config = ConfigDict(frozen=True)

    signals: tuple[SignalModel, ...] = ()


def _masked_page(
    items: tuple[Signal, ...], next_cursor: str | None, principal: Principal
) -> Envelope[Page[dict[str, Any]]]:
    """Mask each signal and rebuild the page envelope with the aggregated masked-field list."""
    from c360.security.serializer import mask_model  # noqa: PLC0415

    masked_items: list[dict[str, Any]] = []
    masked_fields: set[str] = set()
    for index, signal in enumerate(items):
        data, fields = mask_model(_signal_model(signal), principal.field_policy)
        masked_items.append(data)
        masked_fields.update(f"items[{index}].{field}" for field in fields)
    page = Page[dict[str, Any]](items=masked_items, next_cursor=next_cursor)
    return Envelope.of(page, masked_fields=sorted(masked_fields))


async def _set_state(
    signal_id: int, principal: Principal, services: Services, *, action: str
) -> Envelope[DismissResult]:
    signals = services.signals
    if signals is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such signal")
    customer_id = await run_in_threadpool(signals.customer_of, signal_id)
    if customer_id is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such signal")
    # Re-authorize the customer the signal is about, so a state write can never touch a signal for
    # a customer outside the caller's book. A denial is collapsed to 404 so the endpoint never
    # confirms the signal exists to a non-entitled caller.
    await run_in_threadpool(_authorize_customer, principal, services, customer_id)
    if action == "dismiss":
        ok = await run_in_threadpool(signals.dismiss, signal_id, principal.user_id)
        status = "DISMISSED"
    else:
        ok = await run_in_threadpool(signals.acknowledge, signal_id, principal.user_id)
        status = "ACTIONED"
    if not ok:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such signal")
    return Envelope.of(DismissResult(signal_id=signal_id, status=status))


def _authorize_customer(principal: Principal, services: Services, customer_id: str) -> None:
    """The 403-vs-404 gate, reused from the customer routes (design §7.2).

    A denial stays 403 unless the caller's scope would have permitted the id had it existed, in
    which case it becomes 404 — the same oracle-safe rule the customer routes apply.
    """
    try:
        authorize_customer(principal, customer_id, services.customer.repository)
    except EntitlementError:
        if _scope_would_permit(principal.entitlement, customer_id):
            raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such customer") from None
        raise


def _scope_would_permit(scope: object, customer_id: str) -> bool:
    if isinstance(scope, AllScope):
        return True
    if isinstance(scope, BookScope):
        return customer_id in scope.customer_ids
    return False


__all__ = ["router"]
