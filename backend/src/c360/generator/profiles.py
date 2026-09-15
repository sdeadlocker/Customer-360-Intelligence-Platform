"""Financial, credit and risk profiles (tasks 2.2, 2.3).

These three tables are *summed from what was actually emitted*, never drawn. That is not a stylistic
preference; two schema constraints make it the only workable approach.

``financial_profile`` carries ``CHECK (net_worth_cents = total_assets_cents -
total_liabilities_cents)``. Drawing a plausible net worth and a plausible asset total independently
fails that check immediately, and "fixing" it by deriving one from the other while the component
columns
stay independent produces a row that satisfies the constraint and still contradicts the customer's
holdings — a customer with three accounts totalling $40,000 whose profile claims $2M in deposits.
Every
figure here is therefore an aggregate over :attr:`CustomerPlan.accounts` and
:attr:`CustomerPlan.assets`, computed in :class:`c360.domain.money.Cents`, which is exact integer
arithmetic by construction.

``risk_profile`` carries a compound check pairing ``delinquency_status`` with
``current_days_past_due``: ``CURRENT`` requires exactly 0 days, ``DPD_30_59`` requires 30 to 59, and
so
on. The two are therefore derived together in :func:`_delinquency`, from one drawn bucket, rather
than
being drawn separately and reconciled.

Household net worth needs two passes
------------------------------------

``household_net_worth_cents`` is the sum of the members' net worths, so no member's row can be
written
until every member's net worth is known. :func:`generate_profiles` computes all of them first and
emits
afterwards. Requirement 6.2 displays the household rollup next to the individual figure, and the two
disagreeing is exactly the kind of defect that costs a platform its credibility with a relationship
manager.

The thin-file cohort gets no ``credit_profile`` row at all
----------------------------------------------------------

Design §15 gives it "no bureau data". A row of NULLs would satisfy the schema and quietly destroy
the
test case: requirement 4.7 distinguishes absent data from empty data, and the Phase 5 read path has
to
return ``None`` rather than a zeroed profile. :attr:`CohortProfile.fico` being ``None`` is what
encodes
that, and it is checked here rather than inferred from the cohort name.
"""

from __future__ import annotations

from typing import Final

from c360.domain.enums import AccountStatus, AccountType, DelinquencyStatus
from c360.domain.money import Bps, Cents
from c360.generator.context import GeneratorContext
from c360.generator.plan import CustomerPlan, Population
from c360.generator.tables import CREDIT_PROFILE, FINANCIAL_PROFILE, RISK_PROFILE, Dataset

#: Days-past-due ranges per bucket. The keys are the ``CHECK`` constraint's buckets and the values
#: are
#: the ranges it permits, so the two cannot drift apart.
_DPD_RANGES: Final[dict[DelinquencyStatus, tuple[int, int]]] = {
    DelinquencyStatus.CURRENT: (0, 0),
    DelinquencyStatus.DPD_1_29: (1, 29),
    DelinquencyStatus.DPD_30_59: (30, 59),
    DelinquencyStatus.DPD_60_89: (60, 89),
    DelinquencyStatus.DPD_90_PLUS: (90, 240),
}

#: Bucket distribution for the delinquent cohort. Design §15 gives it "DPD buckets 30/60/90+", so
#: the
#: mass sits at 30 and beyond rather than in the 1-29 grace band.
_DELINQUENT_BUCKETS: Final[tuple[tuple[DelinquencyStatus, int], ...]] = (
    (DelinquencyStatus.DPD_1_29, 14),
    (DelinquencyStatus.DPD_30_59, 32),
    (DelinquencyStatus.DPD_60_89, 26),
    (DelinquencyStatus.DPD_90_PLUS, 28),
)

#: Percent of otherwise-healthy customers who are a few days late. Real portfolios have these, and
#: without them the delinquency banner only ever appears for one cohort.
_INCIDENTAL_LATE_PERCENT: Final = 7

#: Percent of the fraud cohort flagged for AML and for PEP.
_FRAUD_AML_PERCENT: Final = 65
_FRAUD_PEP_PERCENT: Final = 30

#: Percent of non-fraud customers who are politically exposed. PEP status is not wrongdoing, and
#: requirement 8.7's non-dismissible indicator has to appear on customers who are otherwise clean.
_BASELINE_PEP_PERCENT: Final = 3

#: FICO-to-risk conversion. A score of 850 maps to roughly 10, a score of 430 to roughly 86.
_RISK_SCORE_DIVISOR: Final = 5.5
_RISK_SCORE_FLOOR: Final = 2.0
_RISK_SCORE_CEILING: Final = 99.0

#: Risk band for a customer with no bureau file: unknown, not good.
_THIN_FILE_RISK: Final = (45, 72)

#: Fraud score bands.
_FRAUD_SCORE_FLAGGED: Final = (62, 97)
_FRAUD_SCORE_NORMAL: Final = (1, 28)

#: Identity and synthetic-identity score bands.
_ID_SCORE_FLAGGED: Final = (55, 95)
_ID_SCORE_NORMAL: Final = (1, 25)

#: Years-on-bureau is capped by how long the customer could plausibly have had credit.
_MIN_CREDIT_AGE: Final = 18


def _financial_totals(plan: CustomerPlan) -> tuple[Cents, Cents, Cents, Cents, Cents, Cents]:
    """Return the six financial-profile aggregates, in exact cents.

    Order: deposits, loans, investments, assets, liabilities, net worth.

    A closed account contributes nothing. It still exists in ``account`` — requirement 5.3 lists
    closed
    holdings — but counting its balance in a rollup would inflate every total, and a closed
    account's
    balance is zero by construction anyway.
    """
    deposits = Cents(0)
    loans = Cents(0)
    investments = Cents(0)
    card_debt = Cents(0)

    for account in plan.accounts:
        if account.status is AccountStatus.CLOSED:
            continue
        if account.account_type is AccountType.DEPOSIT:
            deposits += account.balance
        elif account.account_type is AccountType.LOAN:
            loans += account.balance
        elif account.account_type is AccountType.INVESTMENT:
            investments += account.balance
        elif account.account_type is AccountType.CARD:
            card_debt += account.balance

    asset_values = sum((asset.current_value for asset in plan.assets), Cents(0))

    total_assets = deposits + investments + asset_values
    total_liabilities = loans + card_debt
    # Not drawn, not reconciled: computed, so the CHECK holds by construction.
    net_worth = total_assets - total_liabilities
    return deposits, loans, investments, total_assets, total_liabilities, net_worth


def _card_utilization(plan: CustomerPlan) -> Bps | None:
    """Aggregate card utilization in basis points, or ``None`` when the customer holds no card."""
    balance = Cents(0)
    limit = Cents(0)
    for account in plan.accounts_of(AccountType.CARD):
        if account.status is AccountStatus.CLOSED or account.credit_limit is None:
            continue
        balance += account.balance
        limit += account.credit_limit
    if int(limit) <= 0:
        return None
    # Truncating, to match the SQL definition in design §4.6.
    return balance.ratio_bps(limit)


def _delinquency(ctx: GeneratorContext, plan: CustomerPlan) -> tuple[DelinquencyStatus, int]:
    """Draw a delinquency bucket and a consistent days-past-due value.

    Both come from one draw, because migration ``0001`` requires them to agree and a risk profile
    reading ``CURRENT`` with 45 days past due is the kind of contradiction a risk analyst would stop
    trusting the platform over.
    """
    if plan.profile.delinquent:
        bucket = ctx.weighted(_DELINQUENT_BUCKETS)
    elif ctx.chance(_INCIDENTAL_LATE_PERCENT):
        bucket = DelinquencyStatus.DPD_1_29
    else:
        bucket = DelinquencyStatus.CURRENT
    return bucket, ctx.integer(_DPD_RANGES[bucket])


def _risk_score(ctx: GeneratorContext, plan: CustomerPlan, fico: int | None) -> float:
    """Map a bureau score onto the platform's 0-100 risk scale.

    Design D10 keeps bureau-style scores as consumed data and heuristic scores labelled, so this is
    a
    derived heuristic and it is deliberately monotonic in FICO: a customer with a worse bureau score
    must not come out less risky, or the two numbers sitting side by side in the risk widget
    contradict
    each other.
    """
    if fico is None:
        return float(ctx.integer(_THIN_FILE_RISK))
    base = (850 - fico) / _RISK_SCORE_DIVISOR
    jittered = base + ctx.integer((-4, 4))
    return round(max(_RISK_SCORE_FLOOR, min(_RISK_SCORE_CEILING, jittered)), 2)


def _emit_financial(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    dataset: Dataset,
    household_totals: dict[str, Cents],
) -> None:
    deposits, loans, investments, assets, liabilities, net_worth = _financial_totals(plan)

    dataset.add(
        FINANCIAL_PROFILE,
        (
            plan.customer_id,
            int(deposits),
            int(loans),
            int(investments),
            int(assets),
            int(liabilities),
            int(net_worth),
            (
                int(household_totals[plan.household_id])
                if plan.household_id is not None and plan.household_id in household_totals
                else None
            ),
            int(plan.monthly_income),
            int(plan.monthly_expense) if plan.monthly_expense is not None else None,
            ctx.as_of_iso,
        ),
    )


def _emit_credit(ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset) -> int | None:
    """Emit the bureau profile, or nothing for a customer with no file. Returns the FICO used."""
    if plan.profile.fico is None:
        return None

    fico = ctx.integer(plan.profile.fico)
    utilization = _card_utilization(plan)

    age = (
        (ctx.as_of - plan.date_of_birth).days // 365
        if plan.date_of_birth is not None
        else _MIN_CREDIT_AGE + 5
    )
    max_years = max(0.5, float(age - _MIN_CREDIT_AGE))
    years_on_bureau = round(min(max_years, float(ctx.integer((1, 30)))), 1)

    credit_accounts = len(
        [
            account
            for account in plan.accounts
            if account.account_type in (AccountType.CARD, AccountType.LOAN)
        ]
    )
    exposure = Cents(0)
    for account in plan.accounts:
        if account.account_type is AccountType.CARD and account.credit_limit is not None:
            exposure += account.credit_limit
        elif account.account_type is AccountType.LOAN:
            exposure += account.balance

    dataset.add(
        CREDIT_PROFILE,
        (
            plan.customer_id,
            fico,
            ctx.integer((300, 900)),
            float(ctx.integer((1, 99))),
            int(utilization) if utilization is not None else None,
            years_on_bureau,
            ctx.integer((0, 6)),
            ctx.integer((max(1, credit_accounts), credit_accounts + 8)),
            credit_accounts,
            int(exposure),
            ctx.as_of_iso,
        ),
    )
    return fico


def _emit_risk(
    ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset, fico: int | None
) -> None:
    bucket, days_past_due = _delinquency(ctx, plan)
    plan.days_past_due = days_past_due

    charged_off = any(account.product_status == "CHARGED_OFF" for account in plan.accounts)
    defaulted = charged_off or bucket is DelinquencyStatus.DPD_90_PLUS

    flagged = plan.profile.fraud_flagged
    dataset.add(
        RISK_PROFILE,
        (
            plan.customer_id,
            _risk_score(ctx, plan, fico),
            float(ctx.integer(_FRAUD_SCORE_FLAGGED if flagged else _FRAUD_SCORE_NORMAL)),
            float(ctx.integer(_ID_SCORE_FLAGGED if flagged else _ID_SCORE_NORMAL)),
            float(ctx.integer(_ID_SCORE_FLAGGED if flagged else _ID_SCORE_NORMAL)),
            bucket.value,
            days_past_due,
            1 if defaulted else 0,
            1 if charged_off else 0,
            1 if flagged and ctx.chance(_FRAUD_AML_PERCENT) else 0,
            (
                1
                if (flagged and ctx.chance(_FRAUD_PEP_PERCENT)) or ctx.chance(_BASELINE_PEP_PERCENT)
                else 0
            ),
            ctx.as_of_iso,
        ),
    )


def generate_profiles(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    """Emit the three derived profile tables.

    Two passes: net worth for every customer first, then the rows, because
    ``household_net_worth_cents`` is a sum over members that no single member's row can compute.
    """
    net_worths: dict[str, Cents] = {}
    for plan in population.customers:
        net_worths[plan.customer_id] = _financial_totals(plan)[5]

    household_totals: dict[str, Cents] = {}
    for household in population.households:
        total = Cents(0)
        for member_id in household.member_ids:
            total += net_worths[member_id]
        household_totals[household.household_id] = total

    for plan in population.customers:
        _emit_financial(ctx, plan, dataset, household_totals)
        fico = _emit_credit(ctx, plan, dataset)
        _emit_risk(ctx, plan, dataset, fico)
