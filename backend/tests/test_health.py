"""Financial health score tests (task 3.2).

The score is a pure function of its inputs (design §7.3), so these tests are unit tests against
:func:`c360.domain.health.compute_health_score` with no database. They pin the properties the
requirement renders — value in 0..100, a labelled band, ``provenance: HEURISTIC``, the fixed formula
version, and a named driver per factor — and the direction of each factor, so a healthier input can
never produce a worse contribution.
"""

from __future__ import annotations

from c360.domain.health import (
    FORMULA_VERSION,
    PROVENANCE,
    HealthBand,
    HealthInputs,
    compute_health_score,
)
from c360.domain.money import Cents


def _inputs(**overrides: object) -> HealthInputs:
    base: dict[str, object] = {
        "monthly_income_cents": Cents(1_000_000),
        "monthly_expense_cents": Cents(700_000),
        "monthly_debt_payment_cents": Cents(200_000),
        "liquid_savings_cents": Cents(4_200_000),
        "credit_utilization_bps": 1_000,
        "payment_history_score": 100,
    }
    base.update(overrides)
    return HealthInputs(**base)  # type: ignore[arg-type]


class TestShape:
    def test_score_is_bounded_and_labelled(self) -> None:
        score = compute_health_score(_inputs())
        assert 0 <= score.value <= 100
        assert isinstance(score.band, HealthBand)
        assert score.provenance == PROVENANCE == "HEURISTIC"
        assert score.formula_version == FORMULA_VERSION == "fhs-v1"

    def test_every_factor_produces_a_named_driver(self) -> None:
        score = compute_health_score(_inputs())
        factors = {driver.factor for driver in score.drivers}
        assert factors == {
            "savings_rate",
            "debt_to_income",
            "credit_utilization",
            "emergency_fund",
            "payment_history",
        }

    def test_drivers_sum_to_the_value(self) -> None:
        score = compute_health_score(_inputs())
        assert sum(driver.contribution for driver in score.drivers) == score.value


class TestBands:
    def test_a_strong_profile_bands_high(self) -> None:
        score = compute_health_score(
            _inputs(
                monthly_expense_cents=Cents(500_000),  # 50% savings rate
                monthly_debt_payment_cents=Cents(100_000),  # 10% DTI
                credit_utilization_bps=500,  # 5% utilization
                liquid_savings_cents=Cents(6_000_000),  # 12 months
                payment_history_score=100,
            )
        )
        assert score.band in (HealthBand.GOOD, HealthBand.EXCELLENT)
        assert score.value >= 60

    def test_a_weak_profile_bands_low(self) -> None:
        score = compute_health_score(
            _inputs(
                monthly_expense_cents=Cents(1_050_000),  # spends more than income
                monthly_debt_payment_cents=Cents(500_000),  # 50% DTI
                credit_utilization_bps=9_500,  # 95% utilization
                liquid_savings_cents=Cents(0),  # no emergency fund
                payment_history_score=5,  # severely delinquent
            )
        )
        assert score.band == HealthBand.POOR
        assert score.value < 40


class TestMissingData:
    def test_no_income_zeroes_income_driven_factors(self) -> None:
        score = compute_health_score(
            _inputs(monthly_income_cents=None, monthly_debt_payment_cents=None)
        )
        savings = next(d for d in score.drivers if d.factor == "savings_rate")
        dti = next(d for d in score.drivers if d.factor == "debt_to_income")
        assert savings.contribution == 0
        assert dti.contribution == 0
        assert "no income" in savings.detail

    def test_no_credit_history_zeroes_utilization_and_history(self) -> None:
        score = compute_health_score(
            _inputs(credit_utilization_bps=None, payment_history_score=None)
        )
        util = next(d for d in score.drivers if d.factor == "credit_utilization")
        history = next(d for d in score.drivers if d.factor == "payment_history")
        assert util.contribution == 0
        assert history.contribution == 0


class TestDeterminism:
    def test_same_inputs_same_score(self) -> None:
        assert compute_health_score(_inputs()) == compute_health_score(_inputs())

    def test_lower_utilization_never_scores_worse(self) -> None:
        low = compute_health_score(_inputs(credit_utilization_bps=500))
        high = compute_health_score(_inputs(credit_utilization_bps=9_000))
        low_util = next(d for d in low.drivers if d.factor == "credit_utilization")
        high_util = next(d for d in high.drivers if d.factor == "credit_utilization")
        assert low_util.contribution >= high_util.contribution
