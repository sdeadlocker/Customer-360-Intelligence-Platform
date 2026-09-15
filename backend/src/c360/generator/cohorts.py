"""Persona cohorts and the population split (task 2.1).

Design §15 drives generation from a persona distribution rather than uniform randomness, because the
point of the dataset is exercising every UI state and supplying evaluation ground truth. A uniform
population would produce a hundred nearly-identical mass-market customers, and the delinquency
banner, the AML indicator, the thin-file empty states and the no-relationships graph would never
render in development.

The floor, and why it is 3 rather than 1
---------------------------------------

Design §15 requires "a minimum count so no cohort rounds to zero". Task 2.1 restates it. Taken
literally a floor of 1 satisfies that, and it would still be wrong: design §14.2 fixes the
evaluation panel at "at least 3 per cohort across all 8 cohorts (~24 at the 100-customer default)".
Proportional allocation at 100 customers gives the isolated cohort 1 and the fraud cohort 2, so a
floor of 1 leaves the panel unbuildable for two cohorts and Phase 11 discovers it. The floor is
therefore :data:`MIN_COHORT_SIZE` = 3, which is the smallest value that makes both §15 and §14.2
hold at the default size.

How the deficit is paid for
---------------------------

:func:`allocate` allocates proportionally first, then lifts short cohorts to the floor and takes the
difference from the largest cohorts. Lifting first and distributing the remainder afterwards — the
more obvious order — distorts the shares far more: at 100 customers it would move the mass-market
cohort from 45% to 37%, because every cohort would keep a floor of 3 *plus* its proportional share
of what was left. Paying the deficit out of the largest cohorts costs mass market 3 customers and
leaves every other share untouched.

Shares are integer basis points, not floats, for the same reason money is (design §1.1): the split
has to sum to exactly the requested count, and 0.45 + 0.25 + 0.10 + 0.08 + 0.05 + 0.04 + 0.02 + 0.01
does not sum to 1.0 in binary floating point.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from c360.domain.enums import CustomerSegment, CustomerValue

#: Basis points in the whole population. Mirrors :data:`c360.domain.money.BPS_PER_UNIT`, repeated
#: here so this module does not import a money type to describe a headcount.
_POPULATION_BPS: Final = 10_000

#: Smallest number of customers in any cohort, whenever the requested count can afford it.
#: Three is what design §14.2's evaluation panel needs; see the module docstring.
MIN_COHORT_SIZE: Final = 3


class Cohort(StrEnum):
    """The eight personas in design §15.

    ``HNW`` covers the design's combined "HNW / UHNW" row: it is one cohort that produces customers
    in both the ``HNW`` and ``UHNW`` segments, since the two differ in magnitude rather than in
    which UI states they exercise.

    Declaration order is the allocation and generation order. It is deliberate that this is an
    ``Enum``: iteration order is definition order, which is stable across processes, where a ``set``
    of strings is not — and task 2.1 requires two runs at the same seed to be byte-identical.
    """

    MASS_MARKET = "MASS_MARKET"
    AFFLUENT = "AFFLUENT"
    HNW = "HNW"
    SMALL_BUSINESS = "SMALL_BUSINESS"
    THIN_FILE = "THIN_FILE"
    DELINQUENT = "DELINQUENT"
    FRAUD_FLAGGED = "FRAUD_FLAGGED"
    ISOLATED = "ISOLATED"


#: Weighted choice: a value paired with its relative weight.
Weighted = tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class CohortProfile:
    """Everything the generators need to know about one persona.

    Held as data rather than as branching inside each generator so the whole population model is
    reviewable against design §15 in one place, and so adding a cohort does not mean finding every
    ``if cohort is ...`` in the package.

    Monetary ranges are inclusive ``(low, high)`` bounds in **cents**, matching the column
    convention. Probabilities are integer percent, so no float comparison decides whether a customer
    gets a mortgage.
    """

    cohort: Cohort
    #: Share of the population in basis points. The eight shares sum to 10,000.
    share_bps: int

    # ---------------------------------------------------------------- identity
    segments: tuple[tuple[CustomerSegment, int], ...]
    value_tiers: tuple[tuple[CustomerValue, int], ...]
    #: Tenure with the bank, in whole years.
    tenure_years: tuple[int, int]
    #: Leave demographic and contact fields empty, for requirement 4.7's "no data" states.
    sparse_fields: bool = False

    # ---------------------------------------------------------------- products
    #: Inclusive bounds on the number of accounts. Design §15's "1-3 products" and so on.
    product_count: tuple[int, int] = (1, 3)
    deposit_balance_cents: tuple[int, int] = (50_000, 800_000)
    monthly_income_cents: tuple[int, int] = (250_000, 600_000)
    card_probability: int = 50
    mortgage_probability: int = 0
    investment_probability: int = 0
    business_loan_probability: int = 0
    investment_value_cents: tuple[int, int] = (500_000, 5_000_000)

    # ---------------------------------------------------------------- credit and risk
    #: ``None`` means no bureau file at all — the thin-file cohort gets no ``credit_profile`` row,
    #: which is what makes requirement 4.7's missing-data path real rather than a zero.
    fico: tuple[int, int] | None = (620, 780)
    #: Seed DPD buckets and charge-offs (design §15, requirement 8.1).
    delinquent: bool = False
    #: Seed fraud alerts, failed applications and AML/PEP flags.
    fraud_flagged: bool = False

    # ---------------------------------------------------------------- assets
    property_count: tuple[int, int] = (0, 0)
    vehicle_count: tuple[int, int] = (0, 1)

    # ---------------------------------------------------------------- graph and journey
    #: Isolated customers get no household, no relationships and no offers, so the empty graph and
    #: empty recommendations states are always reachable in the seeded dataset.
    has_household: bool = True
    has_relationships: bool = True
    has_offers: bool = True
    #: Transactions per month, before seasonality. ~200 over 24 months is design §15's target.
    txn_per_month: tuple[int, int] = (7, 10)
    life_event_count: tuple[int, int] = (0, 2)


#: The eight personas of design §15. Shares are the table's percentages in basis points.
COHORT_PROFILES: Final[tuple[CohortProfile, ...]] = (
    CohortProfile(
        cohort=Cohort.MASS_MARKET,
        share_bps=4_500,
        segments=((CustomerSegment.MASS, 100),),
        value_tiers=((CustomerValue.BRONZE, 60), (CustomerValue.SILVER, 40)),
        tenure_years=(1, 12),
        product_count=(1, 3),
        deposit_balance_cents=(20_000, 900_000),
        monthly_income_cents=(280_000, 700_000),
        card_probability=45,
        # Thin credit per design §15: a real but short bureau file.
        fico=(600, 720),
        vehicle_count=(0, 1),
        txn_per_month=(7, 10),
        life_event_count=(0, 2),
    ),
    CohortProfile(
        cohort=Cohort.AFFLUENT,
        share_bps=2_500,
        segments=((CustomerSegment.AFFLUENT, 100),),
        value_tiers=((CustomerValue.SILVER, 30), (CustomerValue.GOLD, 70)),
        tenure_years=(4, 22),
        product_count=(4, 7),
        deposit_balance_cents=(800_000, 9_000_000),
        monthly_income_cents=(750_000, 2_200_000),
        card_probability=95,
        mortgage_probability=75,
        investment_probability=80,
        investment_value_cents=(2_500_000, 40_000_000),
        fico=(700, 820),
        property_count=(1, 1),
        vehicle_count=(1, 2),
        txn_per_month=(8, 11),
        life_event_count=(1, 3),
    ),
    CohortProfile(
        cohort=Cohort.HNW,
        share_bps=1_000,
        segments=((CustomerSegment.HNW, 70), (CustomerSegment.UHNW, 30)),
        value_tiers=((CustomerValue.GOLD, 35), (CustomerValue.PLATINUM, 65)),
        tenure_years=(8, 30),
        product_count=(6, 10),
        deposit_balance_cents=(5_000_000, 60_000_000),
        monthly_income_cents=(2_500_000, 12_000_000),
        card_probability=100,
        mortgage_probability=60,
        investment_probability=100,
        investment_value_cents=(40_000_000, 900_000_000),
        fico=(760, 850),
        # Design §15: "multiple properties, trust structures".
        property_count=(2, 4),
        vehicle_count=(1, 3),
        txn_per_month=(8, 12),
        life_event_count=(1, 3),
    ),
    CohortProfile(
        cohort=Cohort.SMALL_BUSINESS,
        share_bps=800,
        segments=((CustomerSegment.SMALL_BUSINESS, 100),),
        value_tiers=((CustomerValue.SILVER, 40), (CustomerValue.GOLD, 60)),
        tenure_years=(2, 18),
        product_count=(3, 6),
        deposit_balance_cents=(600_000, 18_000_000),
        monthly_income_cents=(600_000, 3_000_000),
        card_probability=85,
        mortgage_probability=20,
        investment_probability=30,
        business_loan_probability=90,
        investment_value_cents=(1_000_000, 15_000_000),
        fico=(660, 800),
        property_count=(0, 1),
        vehicle_count=(1, 2),
        txn_per_month=(9, 13),
        life_event_count=(1, 2),
    ),
    CohortProfile(
        cohort=Cohort.THIN_FILE,
        share_bps=500,
        segments=((CustomerSegment.MASS, 100),),
        value_tiers=((CustomerValue.BRONZE, 100),),
        tenure_years=(0, 2),
        # Deliberately sparse: design §15 "minimal history, no bureau data, sparse fields", which
        # is requirement 4.7's missing-data rendering path.
        sparse_fields=True,
        product_count=(1, 2),
        deposit_balance_cents=(2_000, 120_000),
        monthly_income_cents=(150_000, 320_000),
        card_probability=10,
        fico=None,
        vehicle_count=(0, 0),
        txn_per_month=(3, 6),
        life_event_count=(0, 1),
    ),
    CohortProfile(
        cohort=Cohort.DELINQUENT,
        share_bps=400,
        segments=((CustomerSegment.MASS, 80), (CustomerSegment.AFFLUENT, 20)),
        value_tiers=((CustomerValue.BRONZE, 70), (CustomerValue.SILVER, 30)),
        tenure_years=(1, 14),
        product_count=(2, 4),
        deposit_balance_cents=(1_000, 180_000),
        monthly_income_cents=(220_000, 620_000),
        card_probability=90,
        mortgage_probability=30,
        fico=(430, 600),
        delinquent=True,
        vehicle_count=(0, 1),
        txn_per_month=(7, 10),
        life_event_count=(0, 2),
    ),
    CohortProfile(
        cohort=Cohort.FRAUD_FLAGGED,
        share_bps=200,
        segments=((CustomerSegment.MASS, 50), (CustomerSegment.AFFLUENT, 50)),
        value_tiers=((CustomerValue.SILVER, 50), (CustomerValue.GOLD, 50)),
        tenure_years=(0, 6),
        product_count=(2, 3),
        deposit_balance_cents=(30_000, 2_000_000),
        monthly_income_cents=(300_000, 1_400_000),
        card_probability=100,
        fico=(560, 740),
        fraud_flagged=True,
        vehicle_count=(0, 1),
        txn_per_month=(9, 14),
        life_event_count=(0, 1),
    ),
    CohortProfile(
        cohort=Cohort.ISOLATED,
        share_bps=100,
        segments=((CustomerSegment.MASS, 60), (CustomerSegment.AFFLUENT, 40)),
        value_tiers=((CustomerValue.BRONZE, 50), (CustomerValue.SILVER, 50)),
        tenure_years=(1, 9),
        product_count=(1, 2),
        deposit_balance_cents=(40_000, 1_200_000),
        monthly_income_cents=(260_000, 900_000),
        card_probability=35,
        fico=(620, 770),
        vehicle_count=(0, 1),
        # The point of this cohort: design §15 "no household, no relationships, no offers".
        has_household=False,
        has_relationships=False,
        has_offers=False,
        txn_per_month=(5, 8),
        life_event_count=(0, 1),
    ),
)

#: Lookup by cohort. Built once; keyed by an enum member, so iteration order is definition order.
PROFILE_BY_COHORT: Final[dict[Cohort, CohortProfile]] = {
    profile.cohort: profile for profile in COHORT_PROFILES
}


def _proportional(count: int) -> dict[Cohort, int]:
    """Split ``count`` across the cohorts by share, using the largest-remainder method.

    The same algorithm as :meth:`c360.domain.money.Cents.allocate`, on headcount instead of money,
    and for the same reason: the parts must sum to exactly the total. Ties are broken by declaration
    order so the result does not depend on dictionary or set ordering.
    """
    shares = [profile.share_bps for profile in COHORT_PROFILES]
    floors = [count * share // _POPULATION_BPS for share in shares]
    remainders = [(count * share) % _POPULATION_BPS for share in shares]
    leftover = count - sum(floors)

    order = sorted(range(len(shares)), key=lambda i: (-remainders[i], i))
    for index in order[:leftover]:
        floors[index] += 1
    return {COHORT_PROFILES[i].cohort: floors[i] for i in range(len(COHORT_PROFILES))}


def allocate(count: int, *, minimum: int = MIN_COHORT_SIZE) -> dict[Cohort, int]:
    """Split ``count`` customers across the eight cohorts.

    Every cohort receives at least ``min(minimum, count // 8)`` customers, so the guarantee degrades
    gracefully rather than failing when the requested count cannot afford the full floor.

    Args:
        count: Total customers. Must be at least one per cohort.
        minimum: Per-cohort floor. Defaults to :data:`MIN_COHORT_SIZE`.

    Returns:
        Counts keyed by cohort, in declaration order, summing to exactly ``count``.

    Raises:
        ValueError: ``count`` is smaller than the number of cohorts, or ``minimum`` is negative.
    """
    cohort_count = len(COHORT_PROFILES)
    if count < cohort_count:
        raise ValueError(
            f"count must be at least {cohort_count} so every cohort in design §15 is present, "
            f"got {count}"
        )
    if minimum < 0:
        raise ValueError(f"minimum must not be negative, got {minimum}")

    floor = min(minimum, count // cohort_count)
    allocation = _proportional(count)

    # Lift the short cohorts, then recover the deficit from the largest ones. Largest-first, one at
    # a time, so the cost lands where it distorts the distribution least; ties by declaration order.
    deficit = sum(max(0, floor - size) for size in allocation.values())
    for cohort, size in allocation.items():
        if size < floor:
            allocation[cohort] = floor

    while deficit > 0:
        donors = [cohort for cohort, size in allocation.items() if size > floor]
        if not donors:  # pragma: no cover - unreachable while floor <= count // cohort_count
            raise ValueError(f"cannot give every cohort {floor} customers out of {count}")
        donor = max(
            donors, key=lambda cohort: (allocation[cohort], -list(allocation).index(cohort))
        )
        allocation[donor] -= 1
        deficit -= 1

    return allocation
