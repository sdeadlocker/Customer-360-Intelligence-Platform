"""Revenue-play read endpoints (Phase 22).

The revenue plays turn something the deterministic layer already computes into a *priced*
opportunity rather than an observation. Play 6, fee recovery, is the first of them to land behind a
real endpoint:

``GET /customers/{id}/revenue/fee-recovery`` — charges the bank earned but never collected.

Route shape follows :mod:`c360.api.routes.customers` exactly: authorize the target customer through
the 403-vs-404 gate, run the blocking scan off the event loop, and return through
:func:`~c360.api.masking.masked_envelope` so the field-masking serializer runs on the response and
no handler can skip it. The 403-vs-404 helpers are duplicated here rather than imported, the same
way :mod:`c360.api.routes.signals` duplicates them — a feature module owning its own gate is
preferred over a cross-module import of a private helper.

Why this read is audited when the other deterministic reads are not
-------------------------------------------------------------------

No customer read in :mod:`c360.api.routes.customers` writes an audit record today. This one does.
Requirement 12.6 asks for an audit record on any read of customer data, and a fee-recovery scan is a
distinctly consequential read: its output is a list of charges someone is expected to *act* on, so
"who looked at this, and what did the platform tell them" is exactly the question an auditor will
ask six months later. The record carries the leak types found, never a monetary value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from c360.api.audit import record_access
from c360.api.auth import current_principal
from c360.api.envelope import ApiError, Envelope, ErrorCode
from c360.api.masking import masked_envelope
from c360.api.services import Services, require_services
from c360.security.audit import AuditOutcome
from c360.security.authorization import authorize_customer
from c360.security.entitlement import AllScope, BookScope
from c360.security.errors import EntitlementError

# Imported at runtime, not under TYPE_CHECKING: FastAPI resolves a handler's parameter annotations
# when it builds the route, so a `Principal` that exists only for the type checker makes it treat
# the dependency as a request field and reject every call with a 422.
from c360.security.model import Principal

if TYPE_CHECKING:
    from c360.services.fee_recovery import FeeFinding, FeeRecoveryView

router = APIRouter(tags=["revenue"])

#: Audit action for a fee-recovery scan.
_ACTION_FEE_RECOVERY = "REVENUE_FEE_RECOVERY"


# ================================================================ response models
class FeeFindingModel(BaseModel):
    """One recoverable billing gap.

    ``account_label`` is already partial by construction — product name plus the last four of the
    account number — so the full account number never reaches this layer and no masking rule has to
    remember to redact it. The two monetary fields *are* registered for masking, so a role that sees
    banded balances sees banded recoverable amounts too.
    """

    model_config = ConfigDict(frozen=True)

    finding_id: int
    leak_type: str
    account_label: str
    monthly_cents: int
    annualized_cents: int
    rule_basis: str
    doc_id: str
    evidence: str
    cycles: int
    status: str


class FeeRecoveryResponse(BaseModel):
    """The ``GET /customers/{id}/revenue/fee-recovery`` payload."""

    model_config = ConfigDict(frozen=True)

    monthly_recoverable_cents: int
    annualized_recoverable_cents: int
    findings: tuple[FeeFindingModel, ...] = ()
    as_of_date: str
    source_system: str


# ================================================================ authorization
def _authorize(principal: Principal, services: Services, customer_id: str) -> None:
    """The 403-vs-404 gate (requirement 15.5).

    :func:`authorize_customer` collapses "does not exist" and "not entitled" into one
    ``EntitlementError`` so a probe cannot use the status code as an existence oracle. A denial is
    re-mapped to 404 only when the caller's scope would have permitted the id had it existed,
    because for such a caller there is no hidden row to protect.
    """
    try:
        authorize_customer(principal, customer_id, services.customer.repository)
    except EntitlementError:
        if _scope_would_permit(principal.entitlement, customer_id):
            raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such customer") from None
        raise


def _scope_would_permit(scope: object, customer_id: str) -> bool:
    """Whether ``scope`` would permit ``customer_id`` if it existed, without a query.

    A ``SEGMENT`` scope cannot be decided without the absent customer's segment, so its denial stays
    a 403 — the conservative choice that never turns the status code into an oracle.
    """
    if isinstance(scope, AllScope):
        return True
    if isinstance(scope, BookScope):
        return customer_id in scope.customer_ids
    return False


# ================================================================ fee recovery
@router.get(
    "/customers/{customer_id}/revenue/fee-recovery",
    response_model=Envelope[dict[str, Any]],
    summary="Recoverable fee income: waived, mispriced and unbilled charges",
)
async def get_fee_recovery(
    customer_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, Any]]:
    """Scan the customer's deposit billing against the published fee schedule.

    A customer with clean billing returns an empty finding list and zero totals rather than a 404:
    "nothing recoverable here" is a real answer, and a 404 would make a correctly-billed customer
    indistinguishable from a missing one.
    """
    await run_in_threadpool(_authorize, principal, services, customer_id)

    scan = services.fee_recovery
    if scan is None:
        # The fee schedule could not be loaded. Reported rather than answered with an empty result,
        # because "no leakage found" would misrepresent a broken deployment as good news.
        raise ApiError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "the fee schedule is not available; fee recovery cannot be computed",
        )

    view: FeeRecoveryView = await run_in_threadpool(scan.get_fee_recovery, customer_id)
    _audit(request, principal, customer_id=customer_id, view=view)
    return masked_envelope(_response(view), principal)


def _response(view: FeeRecoveryView) -> FeeRecoveryResponse:
    return FeeRecoveryResponse(
        monthly_recoverable_cents=int(view.monthly_recoverable_cents),
        annualized_recoverable_cents=int(view.annualized_recoverable_cents),
        findings=tuple(_finding_model(finding) for finding in view.findings),
        as_of_date=view.as_of_date,
        source_system=view.source_system,
    )


def _finding_model(finding: FeeFinding) -> FeeFindingModel:
    return FeeFindingModel(
        finding_id=finding.finding_id,
        leak_type=finding.leak_type.value,
        account_label=finding.account_label,
        monthly_cents=int(finding.monthly_cents),
        annualized_cents=int(finding.annualized_cents),
        rule_basis=finding.rule_basis,
        doc_id=finding.doc_id,
        evidence=finding.evidence,
        cycles=finding.cycles,
        status=finding.status,
    )


def _audit(
    request: Request, principal: Principal, *, customer_id: str, view: FeeRecoveryView
) -> None:
    """Record the scan.

    ``fields_accessed`` carries the leak types found, which is field-level information about *what
    kind* of gap was surfaced — never an amount. Design §13.4 and requirement 18.8 keep monetary
    values out of telemetry, and the same restraint applies to the audit payload.
    """
    leak_types = sorted({finding.leak_type.value for finding in view.findings})
    record_access(
        request.app.state.audit_writer,
        principal,
        action=_ACTION_FEE_RECOVERY,
        outcome=AuditOutcome.ALLOWED,
        customer_id=customer_id,
        fields_accessed=leak_types,
        request_path=str(request.url.path),
    )


__all__ = ["router"]
