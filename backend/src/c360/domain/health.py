"""Financial health score — ``fhs-v1`` (task 3.2).

Design §7.3 fixes the formula: a weighted sum of five factors, each normalized to 0..100, then
combined by weight into a single 0..100 score with a labelled band. Every factor's contribution to
the final score is reported as a named driver, so the number is never a black box — a user can see
that a low score is driven by utilization rather than by savings rate.

    financial_health_score (fhs-v1) =
        savings rate        x 25%
      + debt-to-income      x 25%
      + credit utilization  x 20%
      + emergency-fund      x 15%
      + payment history     x 15%

Provenance, and why it is never "SOURCE"
----------------------------------------

This score is a *heuristic*: it is computed from the customer's own figures by a formula this
platform defines, not supplied by a bureau. Design §7.3 (D10) requires it to be labelled
``provenance: HEURISTIC`` and never rendered with the styling a bureau score (FICO, behavior, PID,
SID) gets, because conflating "our opinion" with "the bureau's fact" is exactly the kind of
authority a compliance reviewer would object to. FICO and the rest carry ``provenance: SOURCE`` and
are never touched by this module.

Normalization
-------------

Each factor is mapped to 0..100 by a piecewise-linear ramp between a "poor" and a "healthy"
threshold, clamped at both ends. The thresholds are conventional retail-banking rules of thumb
(e.g. a savings rate at or above 20% is healthy; a DTI at or below 20% is healthy, above 43% is
poor). They live here as named constants rather than in config because the *formula version* is the
unit of change: adjusting a threshold is a new formula (``fhs-v2``), not a runtime tweak, so that a
stored score always means what its ``formula_version`` says it means.

Integer arithmetic
------------------

Inputs are :class:`~c360.domain.money.Cents` (integer minor units) and
:class:`~c360.domain.money.Bps` (integer basis points). Normalization and weighting are done in
integer arithmetic and the final score is a plain ``int`` 0..100, so the score is reproducible to
the unit and carries no float drift, the same property task 3.1 depends on for the derived values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from c360.domain.money import Cents

#: The formula this module implements. Stored on every score so a value is interpretable against the
#: exact thresholds that produced it. A threshold change is a new version, never a silent edit.
FORMULA_VERSION: Final = "fhs-v1"

#: Provenance is fixed. A heuristic score is never rendered as a bureau score (design §7.3, D10).
PROVENANCE: Final = "HEURISTIC"


class HealthBand(StrEnum):
    """The labelled bucket a score falls into. Matches the CHECK on ``derived_health.band``."""

    POOR = "POOR"
    FAIR = "FAIR"
    GOOD = "GOOD"
    EXCELLENT = "EXCELLENT"


#: Factor weights, in percent, summing to 100 (design §7.3). Held as integers so the weighted sum is
#: exact: a weighted score is Σ(normalized_factor x weight) // MAX_SCORE.
_WEIGHT_SAVINGS_RATE: Final = 25
_WEIGHT_DTI: Final = 25
_WEIGHT_UTILIZATION: Final = 20
_WEIGHT_EMERGENCY_FUND: Final = 15
_WEIGHT_PAYMENT_HISTORY: Final = 15

#: The score is a percentage, so the weights must sum to it. Checked at import so a future edit that
#: unbalances them fails loudly here rather than silently capping the score below 100.
MAX_SCORE: Final = 100
_WEIGHT_TOTAL: Final = (
    _WEIGHT_SAVINGS_RATE
    + _WEIGHT_DTI
    + _WEIGHT_UTILIZATION
    + _WEIGHT_EMERGENCY_FUND
    + _WEIGHT_PAYMENT_HISTORY
)
if _WEIGHT_TOTAL != MAX_SCORE:
    raise ValueError(f"fhs-v1 weights must sum to {MAX_SCORE}, got {_WEIGHT_TOTAL}")

#: Band boundaries on the 0..100 score. Lower bound inclusive.
_BAND_FAIR_MIN: Final = 40
_BAND_GOOD_MIN: Final = 60
_BAND_EXCELLENT_MIN: Final = 80


def _band_for(value: int) -> HealthBand:
    if value >= _BAND_EXCELLENT_MIN:
        return HealthBand.EXCELLENT
    if value >= _BAND_GOOD_MIN:
        return HealthBand.GOOD
    if value >= _BAND_FAIR_MIN:
        return HealthBand.FAIR
    return HealthBand.POOR


def _ramp(value: float, *, poor: float, healthy: float) -> int:
    """Map ``value`` to 0..100 by a linear ramp between ``poor`` and ``healthy``, clamped.

    When ``healthy > poor`` the ramp is increasing (more is better, e.g. savings rate); when
    ``healthy < poor`` it is decreasing (less is better, e.g. DTI, utilization). The two directions
    are the same expression because the clamp handles the ordering.
    """
    if healthy == poor:
        raise ValueError("ramp thresholds must differ")
    fraction = (value - poor) / (healthy - poor)
    clamped = max(0.0, min(1.0, fraction))
    return round(clamped * MAX_SCORE)


@dataclass(frozen=True, slots=True)
class Driver:
    """One factor's contribution to the score. ``contribution`` is in score points (0..weight)."""

    factor: str
    contribution: int
    detail: str


@dataclass(frozen=True, slots=True)
class HealthScore:
    """A computed ``fhs-v1`` score with its band, provenance and named drivers (design §7.3)."""

    value: int
    band: HealthBand
    drivers: tuple[Driver, ...]
    formula_version: str = FORMULA_VERSION
    provenance: str = PROVENANCE


@dataclass(frozen=True, slots=True)
class HealthInputs:
    """The figures ``fhs-v1`` scores. All monetary values are trailing-month or current snapshots.

    A ``None`` factor is one the data does not support (e.g. no income on file). It contributes zero
    and its driver says so, rather than being silently treated as healthy or as poor.
    """

    monthly_income_cents: Cents | None
    monthly_expense_cents: Cents | None
    monthly_debt_payment_cents: Cents | None
    liquid_savings_cents: Cents | None
    credit_utilization_bps: int | None
    #: Payment history as a 0..100 figure already (e.g. derived from delinquency). ``None`` when no
    #: credit history exists to judge.
    payment_history_score: int | None


def _savings_rate_factor(inputs: HealthInputs) -> Driver:
    income = inputs.monthly_income_cents
    expense = inputs.monthly_expense_cents
    if income is None or int(income) <= 0 or expense is None:
        return Driver("savings_rate", 0, "no income on file")
    saved = int(income) - int(expense)
    rate_pct = (saved / int(income)) * 100
    normalized = _ramp(rate_pct, poor=0.0, healthy=20.0)
    contribution = normalized * _WEIGHT_SAVINGS_RATE // MAX_SCORE
    return Driver("savings_rate", contribution, f"{round(rate_pct)}% of income")


def _dti_factor(inputs: HealthInputs) -> Driver:
    income = inputs.monthly_income_cents
    debt = inputs.monthly_debt_payment_cents
    if income is None or int(income) <= 0 or debt is None:
        return Driver("debt_to_income", 0, "no income on file")
    dti_pct = (int(debt) / int(income)) * 100
    # Lower is better: 20% or below is healthy, 43% (the conventional mortgage ceiling) is poor.
    normalized = _ramp(dti_pct, poor=43.0, healthy=20.0)
    contribution = normalized * _WEIGHT_DTI // MAX_SCORE
    return Driver("debt_to_income", contribution, f"{round(dti_pct)}% of income")


def _utilization_factor(inputs: HealthInputs) -> Driver:
    util_bps = inputs.credit_utilization_bps
    if util_bps is None:
        return Driver("credit_utilization", 0, "no revolving credit")
    util_pct = util_bps / 100
    # Lower is better: 10% or below is healthy, 90% or above is poor.
    normalized = _ramp(util_pct, poor=90.0, healthy=10.0)
    contribution = normalized * _WEIGHT_UTILIZATION // MAX_SCORE
    return Driver("credit_utilization", contribution, f"{round(util_pct)}% utilized")


def _emergency_fund_factor(inputs: HealthInputs) -> Driver:
    savings = inputs.liquid_savings_cents
    expense = inputs.monthly_expense_cents
    if savings is None or expense is None or int(expense) <= 0:
        return Driver("emergency_fund", 0, "insufficient data")
    months = int(savings) / int(expense)
    # Zero months is poor, six months of expenses covered is healthy.
    normalized = _ramp(months, poor=0.0, healthy=6.0)
    contribution = normalized * _WEIGHT_EMERGENCY_FUND // MAX_SCORE
    return Driver("emergency_fund", contribution, f"{months:.1f} months of expenses")


def _payment_history_factor(inputs: HealthInputs) -> Driver:
    score = inputs.payment_history_score
    if score is None:
        return Driver("payment_history", 0, "no credit history")
    normalized = max(0, min(MAX_SCORE, score))
    contribution = normalized * _WEIGHT_PAYMENT_HISTORY // MAX_SCORE
    return Driver("payment_history", contribution, f"{normalized}/100")


def compute_health_score(inputs: HealthInputs) -> HealthScore:
    """Compute the ``fhs-v1`` score, its band and its drivers from ``inputs`` (design §7.3).

    Deterministic and pure: the same inputs always produce the same score, which is what lets the
    recompute job store it and a test recompute it and compare.
    """
    drivers = (
        _savings_rate_factor(inputs),
        _dti_factor(inputs),
        _utilization_factor(inputs),
        _emergency_fund_factor(inputs),
        _payment_history_factor(inputs),
    )
    value = sum(driver.contribution for driver in drivers)
    # Contributions are each floored, so the sum is already within 0..100; clamp defensively.
    value = max(0, min(MAX_SCORE, value))
    return HealthScore(value=value, band=_band_for(value), drivers=drivers)
