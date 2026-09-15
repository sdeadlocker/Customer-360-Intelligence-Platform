"""``RiskService`` — risk profile, banding, exposure and alerts (task 5.4, design §6.1).

The repository returns the raw risk profile and the exposure sum; the service derives everything a
risk view needs on top of that: a coarse band for the risk score, a severity-ordered list of alerts
synthesized from the profile's indicators, and the compliance flag requirement 8.4 makes
non-dismissible.

Banding is not masking. The field-masking serializer (task 4.4) decides whether a *role* sees the
numeric score at all (requirement 8.7); the band here is a modelling convenience computed from the
score for every caller, and a role without score entitlement simply never receives the number the
band was derived from. The two do not overlap — one is authorization, one is presentation.

The compliance indicator (requirement 8.4)
------------------------------------------

An AML or PEP flag produces a ``COMPLIANCE`` alert that is marked ``dismissible = False``. It is a
server-driven flag, not a UI preference: the API states that the indicator cannot be dismissed, and
a
client that hides it is departing from the contract. It always sorts first, because a compliance
obligation outranks a delinquency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from c360.domain.enums import DelinquencyStatus
from c360.domain.money import Cents

if TYPE_CHECKING:
    from c360.data.repositories.risk import SqliteRiskRepository
    from c360.domain.models import RiskProfile


class RiskBand(StrEnum):
    """A coarse bucket for the risk score, for callers that see the band but not the number."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"


class AlertSeverity(IntEnum):
    """Alert severity, ordered so a higher value sorts first (requirement 8.3)."""

    INFO = 1
    WARNING = 2
    CRITICAL = 3


@dataclass(frozen=True, slots=True)
class RiskAlert:
    """One severity-ranked alert derived from the risk profile (requirement 8.2, 8.3).

    ``dismissible`` is ``False`` only for the compliance indicator (requirement 8.4). ``category``
    groups alerts for the UI; ``detail`` is a short, value-free label safe to render and to log.
    """

    category: str
    severity: AlertSeverity
    detail: str
    dismissible: bool


@dataclass(frozen=True, slots=True)
class RiskView:
    """The risk read model behind requirements 8.1-8.6.

    ``profile`` carries the raw scores and flags (masked at the API boundary per role); ``band`` is
    the coarse bucket; ``alerts`` are severity-ordered, compliance first; ``credit_exposure_cents``
    is loan balances plus card limits (design §4.6). ``requires_compliance_indicator`` is surfaced
    at
    the top level so a client renders the persistent banner without walking the alert list.
    """

    profile: RiskProfile
    band: RiskBand
    credit_exposure_cents: Cents
    alerts: tuple[RiskAlert, ...]
    requires_compliance_indicator: bool


# Risk-score band boundaries on the profile's 0..100 scale. Lower bound inclusive.
_BAND_MODERATE_MIN = 25.0
_BAND_ELEVATED_MIN = 50.0
_BAND_HIGH_MIN = 75.0

#: Which delinquency buckets warrant which severity.
_DELINQUENCY_SEVERITY: dict[DelinquencyStatus, AlertSeverity] = {
    DelinquencyStatus.DPD_1_29: AlertSeverity.WARNING,
    DelinquencyStatus.DPD_30_59: AlertSeverity.WARNING,
    DelinquencyStatus.DPD_60_89: AlertSeverity.CRITICAL,
    DelinquencyStatus.DPD_90_PLUS: AlertSeverity.CRITICAL,
}


def _band_for(score: float | None) -> RiskBand:
    if score is None or score < _BAND_MODERATE_MIN:
        return RiskBand.LOW
    if score < _BAND_ELEVATED_MIN:
        return RiskBand.MODERATE
    if score < _BAND_HIGH_MIN:
        return RiskBand.ELEVATED
    return RiskBand.HIGH


class RiskService:
    """Risk profile, banding, exposure and alerts over the risk repository."""

    __slots__ = ("_repository",)

    def __init__(self, repository: SqliteRiskRepository) -> None:
        self._repository = repository

    def get_risk(self, customer_id: str) -> RiskView | None:
        """The full risk view, or ``None`` when no risk profile exists for the customer."""
        profile = self._repository.get_risk_profile(customer_id)
        if profile is None:
            return None
        exposure = Cents(self._repository.total_credit_exposure_cents(customer_id))
        return RiskView(
            profile=profile,
            band=_band_for(profile.risk_score),
            credit_exposure_cents=exposure,
            alerts=self._alerts(profile),
            requires_compliance_indicator=profile.requires_compliance_indicator,
        )

    def get_exposure_cents(self, customer_id: str) -> Cents:
        """Loan balances plus card limits (requirement 8.5). Zero, never ``None``, for no credit."""
        return Cents(self._repository.total_credit_exposure_cents(customer_id))

    def _alerts(self, profile: RiskProfile) -> tuple[RiskAlert, ...]:
        """Synthesize alerts from the profile's indicators, ordered by severity (requirement
        8.3)."""
        alerts: list[RiskAlert] = []

        # Compliance first and non-dismissible (requirement 8.4).
        if profile.aml_flag:
            alerts.append(
                RiskAlert(
                    "COMPLIANCE", AlertSeverity.CRITICAL, "AML flag active", dismissible=False
                )
            )
        if profile.pep_flag:
            alerts.append(
                RiskAlert(
                    "COMPLIANCE",
                    AlertSeverity.CRITICAL,
                    "PEP flag active",
                    dismissible=False,
                )
            )

        if profile.chargeoff_indicator:
            alerts.append(
                RiskAlert("CREDIT", AlertSeverity.CRITICAL, "Charge-off recorded", dismissible=True)
            )
        if profile.default_indicator:
            alerts.append(
                RiskAlert(
                    "CREDIT", AlertSeverity.CRITICAL, "Default indicator set", dismissible=True
                )
            )

        delinquency = profile.delinquency_status
        if delinquency is not None and delinquency is not DelinquencyStatus.CURRENT:
            severity = _DELINQUENCY_SEVERITY.get(delinquency, AlertSeverity.WARNING)
            alerts.append(
                RiskAlert(
                    "DELINQUENCY",
                    severity,
                    f"Delinquency {delinquency.value} ({profile.current_days_past_due} DPD)",
                    dismissible=True,
                )
            )

        if profile.fraud_score is not None and profile.fraud_score >= _BAND_HIGH_MIN:
            alerts.append(
                RiskAlert("FRAUD", AlertSeverity.WARNING, "Elevated fraud score", dismissible=True)
            )

        # Compliance alerts must lead regardless of the delinquency/credit severities, so sort by
        # severity descending with compliance forced ahead of an equal-severity peer.
        alerts.sort(
            key=lambda alert: (-alert.severity, alert.category != "COMPLIANCE", alert.detail)
        )
        return tuple(alerts)


__all__ = ["AlertSeverity", "RiskAlert", "RiskBand", "RiskService", "RiskView"]
