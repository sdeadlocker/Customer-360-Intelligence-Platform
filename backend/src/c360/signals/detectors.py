"""Deterministic signal detectors over the existing computed layer (task 17.2).

Each detector reuses the deterministic services the REST API and tools already use — nothing here
runs an LLM. A model is only ever needed to *narrate* a signal (Phase 18's briefing), never to
*detect* one, which is what keeps the feed reproducible and testable on ``LLM_PROVIDER=mock``.

The four detectors map one-to-one onto the cohorts the Phase 17 gate names:

* :func:`detect_risk_band` — a risk band of ELEVATED or HIGH, corroborated by the risk service's
  own severity-ordered alerts (the delinquent cohort). "Movement" is expressed as the band the
  customer now sits in together with the concrete indicators (delinquency bucket, charge-off,
  default) that put them there — there is no stored band-history table, so the current elevated
  band *is* the actionable state, and its evidence is inspectable.
* :func:`detect_aml_pep` — an active AML or PEP flag (the fraud-flagged cohort). Always CRITICAL and
  the reason the risk service marks the compliance indicator non-dismissible.
* :func:`detect_large_deposit` — the single largest credit in the trailing window above the
  configured floor (the HNW large-deposit cohort). Opens a cross-sell window; value-at-stake is the
  deposit amount.
* :func:`detect_life_event` — a detected life event, corroborated by the transactions the generator
  seeded alongside it (task 2.6), so an inference is verifiable rather than asserted.

Every detector emits a :class:`~c360.signals.models.DetectedSignal` carrying typed evidence with
fact citations and a deterministic ``dedup_key`` so re-running detection updates rather than
duplicates a live signal.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import TYPE_CHECKING

from c360.services.risk import RiskBand
from c360.signals.models import (
    Citation,
    DetectedSignal,
    Evidence,
    Severity,
    SignalType,
)

if TYPE_CHECKING:
    from c360.domain.models import Customer
    from c360.services.financial import FinancialService
    from c360.services.journey import JourneyService
    from c360.services.risk import RiskService


def _dedup(customer_id: str, signal_type: SignalType, discriminator: str) -> str:
    """A stable identity for a live concern (task 17.1).

    Deterministic — customer, type and the specific condition — and never a timestamp, so the same
    still-true condition produces the same key across runs and the store upserts in place.
    """
    return f"{customer_id}:{signal_type.value}:{discriminator}"


def _citation(entity_type: str, entity_id: str, field: str, as_of: date) -> Citation:
    return Citation(
        entity_type=entity_type, entity_id=entity_id, field=field, as_of=as_of.isoformat()
    )


def _base_details(customer: Customer) -> dict[str, str]:
    """The qualifiers every signal carries: the segment (for scoping/metric) and the band-free
    customer segment name the UI shows as a chip. No monetary value, no PII beyond the id."""
    return {"segment": str(customer.customer_segment)}


# ---------------------------------------------------------------- risk band movement
def detect_risk_band(
    customer: Customer, risk: RiskService, as_of: date
) -> Iterator[DetectedSignal]:
    """Emit a signal when the customer's risk band is ELEVATED or HIGH (task 17.2).

    Severity tracks the band: HIGH → CRITICAL, ELEVATED → WARNING. The evidence carries the band,
    the numeric risk score's fact citation, and the risk service's own dismissible alerts (the
    delinquency/charge-off/default indicators) as the corroborating movement, so an RM sees *why*
    the band is where it is. AML/PEP is handled by its own detector, so compliance alerts are not
    duplicated here.
    """
    view = risk.get_risk(customer.customer_id)
    if view is None or view.band not in (RiskBand.ELEVATED, RiskBand.HIGH):
        return
    severity = Severity.CRITICAL if view.band is RiskBand.HIGH else Severity.WARNING
    drivers = [alert.detail for alert in view.alerts if alert.category != "COMPLIANCE"]
    details = _base_details(customer)
    details["band"] = str(view.band)
    if drivers:
        details["drivers"] = "; ".join(drivers[:3])
    yield DetectedSignal(
        customer_id=customer.customer_id,
        signal_type=SignalType.RISK_BAND_UP,
        severity=severity,
        # Value-at-stake is the credit exposure the elevated risk sits against.
        value_at_stake_cents=int(view.credit_exposure_cents),
        evidence=Evidence(
            summary=f"Risk band {view.band.value}",
            citations=(_citation("risk_profile", customer.customer_id, "risk_score", as_of),),
            details=details,
        ),
        as_of=as_of,
        # Band is part of the key so a move between elevated bands re-scores the same live signal.
        dedup_key=_dedup(customer.customer_id, SignalType.RISK_BAND_UP, str(view.band)),
    )


# ---------------------------------------------------------------- AML / PEP
def detect_aml_pep(customer: Customer, risk: RiskService, as_of: date) -> Iterator[DetectedSignal]:
    """Emit a CRITICAL signal when an AML or PEP flag is active (task 17.2, requirement 8.4)."""
    view = risk.get_risk(customer.customer_id)
    if view is None or not view.requires_compliance_indicator:
        return
    flags = [alert.detail for alert in view.alerts if alert.category == "COMPLIANCE"]
    details = _base_details(customer)
    if flags:
        details["flags"] = "; ".join(flags)
    yield DetectedSignal(
        customer_id=customer.customer_id,
        signal_type=SignalType.AML_PEP_FLAG,
        severity=Severity.CRITICAL,
        value_at_stake_cents=int(view.credit_exposure_cents),
        evidence=Evidence(
            summary="Compliance flag active",
            citations=(_citation("risk_profile", customer.customer_id, "aml_flag", as_of),),
            details=details,
        ),
        as_of=as_of,
        dedup_key=_dedup(customer.customer_id, SignalType.AML_PEP_FLAG, "active"),
    )


# ---------------------------------------------------------------- large deposit
def detect_large_deposit(
    customer: Customer,
    financial: FinancialService,
    as_of: date,
    *,
    threshold_cents: int,
) -> Iterator[DetectedSignal]:
    """Emit a signal for the largest single credit above the configured floor (task 17.2).

    A large inflow is a cross-sell window (invest the cash, a term deposit, a wealth review). The
    signal is keyed on the transaction id so the same deposit is one live signal, and its
    value-at-stake is the deposit amount. Uses the financial service's transaction read — the same
    one expense analytics is built on — so detection reuses the deterministic layer.
    """
    transactions = financial.get_transactions(customer.customer_id, limit=500)
    large_credits = [txn for txn in transactions if int(txn.amount_cents) >= threshold_cents]
    if not large_credits:
        return
    # The largest, then the most recent, deterministically.
    largest = max(large_credits, key=lambda txn: (int(txn.amount_cents), txn.transaction_date))
    details = _base_details(customer)
    details["window"] = "opened"
    yield DetectedSignal(
        customer_id=customer.customer_id,
        signal_type=SignalType.LARGE_DEPOSIT,
        severity=Severity.INFO,
        value_at_stake_cents=int(largest.amount_cents),
        evidence=Evidence(
            summary="Large deposit received",
            citations=(
                _citation("txn", largest.transaction_id, "amount_cents", largest.transaction_date),
            ),
            details=details,
        ),
        as_of=as_of,
        dedup_key=_dedup(customer.customer_id, SignalType.LARGE_DEPOSIT, largest.transaction_id),
    )


# ---------------------------------------------------------------- life event
def detect_life_event(
    customer: Customer, journey: JourneyService, as_of: date
) -> Iterator[DetectedSignal]:
    """Emit a signal per detected life event (task 17.2).

    Life events are seeded with corroborating transactions (task 2.6), so the inference is
    verifiable. Severity is WARNING for an inferred event (an inference an RM should confirm) and
    INFO for a system-of-record one. Each event is keyed on its own id so the feed carries one
    signal per event.
    """
    for event in journey.get_life_events(customer.customer_id):
        severity = Severity.WARNING if event.is_inferred else Severity.INFO
        details = _base_details(customer)
        details["life_event"] = str(event.life_event_type)
        details["inferred"] = "true" if event.is_inferred else "false"
        yield DetectedSignal(
            customer_id=customer.customer_id,
            signal_type=SignalType.LIFE_EVENT,
            severity=severity,
            value_at_stake_cents=0,
            evidence=Evidence(
                summary=f"Life event: {event.life_event_type.value}",
                citations=(
                    _citation(
                        "life_event", event.life_event_id, "life_event_type", event.event_date
                    ),
                ),
                details=details,
            ),
            as_of=as_of,
            dedup_key=_dedup(customer.customer_id, SignalType.LIFE_EVENT, event.life_event_id),
        )


__all__ = [
    "detect_aml_pep",
    "detect_large_deposit",
    "detect_life_event",
    "detect_risk_band",
]
