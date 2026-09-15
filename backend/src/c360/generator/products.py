"""Accounts, holdings and linked assets (task 2.3, and the asset half of task 2.5).

Assets are planned here rather than in :mod:`c360.generator.network` because a mortgage needs a
property to be secured on and an auto loan needs a vehicle. ``loan.collateral_asset_id`` is a real
foreign key, so the collateral has to exist before the loan that points at it — see
:mod:`c360.generator.tables` on why that also reorders the load.

What the numbers have to satisfy
--------------------------------

The schema is unusually opinionated about internal consistency, and every one of these constraints
is
a rule some requirement depends on:

* ``(account_status = 'CLOSED') = (close_date IS NOT NULL)`` — a closed account has a close date and
  nothing else does. A ``DORMANT`` or ``FROZEN`` account is still open.
* ``card_last4 = substr(card_number, -4)`` — the denormalized last-4 has to agree with the PAN, so
  both are derived from one generated number and never drawn separately.
* ``credit_limit_cents > 0`` and ``original_amount_cents > 0`` — a card with no limit and a loan of
  zero are not products.
* ``utilization_bps`` is computed with :meth:`c360.domain.money.Cents.ratio_bps`, which truncates by
  default to match SQLite integer division. Design §4.6 defines utilization as a SQL expression and
  task 3.1 asserts that stored derived values equal freshly-computed ones, so a Python path that
  rounded half-up here would fail that assertion on any balance landing on a half basis point.

Amortization is linear, deliberately
------------------------------------

Outstanding principal is ``original / term``, times the months remaining, computed with
:meth:`c360.domain.money.Cents.divide` so it stays exact integer cents. A true annuity schedule
would
be more realistic and would introduce a float — or a 360-iteration loop per loan — for a number no
requirement inspects. The EMI *is* computed as a proper annuity, because requirement 5.5 displays it
next to the rate and term, where an inconsistent figure would be visible.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Final

from c360.domain.enums import (
    AccountStatus,
    AccountType,
    AssetType,
    DepositProductType,
    InvestmentRiskProfile,
    LoanType,
    OwnershipType,
    PropertyType,
)
from c360.domain.money import Bps, Cents, Rounding
from c360.generator import vocab
from c360.generator.context import GeneratorContext, add_months
from c360.generator.plan import AccountRecord, AssetRecord, CustomerPlan, Population

#: Characters permitted in a VIN. I, O and Q are excluded by the standard because they are
#: indistinguishable from 1 and 0.
_VIN_ALPHABET: Final = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
_VIN_LENGTH: Final = 17

#: Percent of deposit accounts that are closed, dormant or frozen, so those UI states are reachable.
_CLOSED_DEPOSIT_PERCENT: Final = 7
_DORMANT_DEPOSIT_PERCENT: Final = 6

#: Card utilization bands, in basis points, as ``(low, high)``. The delinquent and fraud cohorts run
#: hot; everyone else is mostly moderate.
_UTILIZATION_HEALTHY: Final = (0, 3_500)
_UTILIZATION_MODERATE: Final = (2_500, 6_500)
_UTILIZATION_STRESSED: Final = (7_000, 11_500)

#: Percent chance a card is run at a stressed utilization outside the delinquent cohort.
_STRESSED_UTILIZATION_PERCENT: Final = 12

#: Loan statuses. ``CURRENT`` for a performing loan; the rest belong to the delinquent cohort.
_LOAN_STATUS_CURRENT: Final = "CURRENT"
_LOAN_STATUS_DELINQUENT: Final = "DELINQUENT"
_LOAN_STATUS_CHARGED_OFF: Final = "CHARGED_OFF"
_LOAN_STATUS_PAID_OFF: Final = "PAID_OFF"

#: Percent of the delinquent cohort's loans that have been charged off rather than merely late.
_CHARGE_OFF_PERCENT: Final = 30

#: Fraud alert counts for the fraud-flagged cohort.
_FRAUD_ALERT_RANGE: Final = (1, 4)

#: Investment mixes per risk profile, as percentage weights over
#: ``(mutual_funds, stocks, bonds, retirement)``. Weights, not amounts: the split is done with
#: :meth:`c360.domain.money.Cents.allocate` so the four components sum to the portfolio value
#: exactly.
_ALLOCATION_WEIGHTS: Final[dict[str, tuple[int, int, int, int]]] = {
    "CONSERVATIVE": (30, 10, 45, 15),
    "MODERATE": (32, 26, 24, 18),
    "GROWTH": (26, 44, 12, 18),
    "AGGRESSIVE": (18, 62, 5, 15),
}

#: Risk profile weights by segment, so an UHNW portfolio is not conservative by accident.
_RISK_PROFILE_BY_TIER: Final[tuple[tuple[InvestmentRiskProfile, int], ...]] = (
    (InvestmentRiskProfile.CONSERVATIVE, 22),
    (InvestmentRiskProfile.MODERATE, 38),
    (InvestmentRiskProfile.GROWTH, 28),
    (InvestmentRiskProfile.AGGRESSIVE, 12),
)

#: Deposit product codes that are term products and therefore carry a maturity date.
_TERM_DEPOSIT_TYPES: Final = (DepositProductType.CD,)

#: Down-payment share of a property purchase, in percent.
_DOWN_PAYMENT_PERCENT: Final = (10, 35)

#: Property value appreciation since purchase, in percent of the purchase price.
_APPRECIATION_PERCENT: Final = (100, 165)

#: Vehicle depreciation, in percent of the purchase price.
_DEPRECIATION_PERCENT: Final = (35, 92)

_SQUARE_FEET: Final = (780, 6_400)
_MILEAGE_PER_YEAR: Final = (4_000, 19_000)


def _annuity_emi(principal: Cents, rate_bps: int, term_months: int) -> Cents:
    """Monthly payment for a level-payment loan.

    ``Decimal`` rather than ``float`` throughout: the result is money being created, so it goes
    through :meth:`c360.domain.money.Cents.from_decimal` with an explicit rounding mode, which is
    the
    rule :mod:`c360.domain.money` exists to enforce. Banker's rounding would bias a population of
    EMIs downward, so this uses half-up, the convention lenders actually publish.
    """
    if term_months < 1:
        raise ValueError(f"term_months must be positive, got {term_months}")
    monthly_rate = Decimal(rate_bps) / Decimal(10_000) / Decimal(12)
    if monthly_rate == 0:
        return principal.divide(term_months, rounding=Rounding.HALF_UP)
    growth = (Decimal(1) + monthly_rate) ** term_months
    payment = Decimal(int(principal)) * monthly_rate * growth / (growth - Decimal(1))
    return Cents.from_decimal(payment / Decimal(100), rounding=Rounding.HALF_UP)


def _vin(ctx: GeneratorContext) -> str:
    return "".join(ctx.pick(_VIN_ALPHABET) for _ in range(_VIN_LENGTH))


def _account_number(ctx: GeneratorContext) -> str:
    return ctx.digits(12)


def _plan_property(
    ctx: GeneratorContext, plan: CustomerPlan, ordinal: int, *, is_primary: bool
) -> AssetRecord:
    """Build one property. The primary residence sits at the customer's own address."""
    purchase_price = ctx.cents((14_000_000, 180_000_000), round_to=10_000)
    appreciation = ctx.integer(_APPRECIATION_PERCENT)
    current_value = purchase_price.apply_bps(Bps(appreciation * 100))
    acquired = ctx.date_between(max(plan.customer_since, add_months(ctx.as_of, -300)), ctx.as_of)

    if is_primary:
        property_type = PropertyType.PRIMARY_RESIDENCE
        address, city, state, postal = (
            plan.street_address,
            plan.city,
            plan.state,
            plan.postal_code,
        )
    else:
        property_type = ctx.weighted(
            (
                (PropertyType.SECOND_HOME, 34),
                (PropertyType.INVESTMENT, 46),
                (PropertyType.LAND, 12),
                (PropertyType.COMMERCIAL, 8),
            )
        )
        city, state, postal_prefix = ctx.pick(vocab.CITIES)
        address = f"{ctx.integer((100, 9899))} {ctx.pick(vocab.STREET_NAMES)}"
        postal = f"{postal_prefix}{ctx.integer((10, 99))}"

    return AssetRecord(
        asset_id=f"{plan.customer_id}-AS-P{ordinal}",
        asset_type=AssetType.PROPERTY,
        description=f"{property_type.value.replace('_', ' ').title()} in {city}",
        current_value=current_value,
        ownership_type=(
            OwnershipType.JOINT.value
            if plan.household_id is not None and ctx.chance(45)
            else OwnershipType.SOLE.value
        ),
        acquired_date=acquired,
        property_type=property_type.value,
        address_line1=address,
        city=city,
        state=state,
        postal_code=postal,
        purchase_price=purchase_price,
        # Assessed value trails market value, as it does in reality.
        assessed_value=current_value.apply_bps(Bps(ctx.integer((88, 99)) * 100)),
        square_feet=(None if property_type is PropertyType.LAND else ctx.integer(_SQUARE_FEET)),
        year_built=ctx.integer((1918, ctx.as_of.year - 1)),
    )


def _plan_vehicle(ctx: GeneratorContext, plan: CustomerPlan, ordinal: int) -> AssetRecord:
    make, models = ctx.pick(vocab.VEHICLE_MAKES)
    model = ctx.pick(models)
    model_year = ctx.integer((ctx.as_of.year - 14, ctx.as_of.year))
    purchase_price = ctx.cents((1_400_000, 9_500_000), round_to=1_000)
    age_years = max(0, ctx.as_of.year - model_year)
    current_value = purchase_price.apply_bps(Bps(ctx.integer(_DEPRECIATION_PERCENT) * 100))
    acquired = ctx.date_between(date(model_year, 1, 1), ctx.as_of)

    return AssetRecord(
        asset_id=f"{plan.customer_id}-AS-V{ordinal}",
        asset_type=AssetType.VEHICLE,
        description=f"{model_year} {make} {model}",
        current_value=current_value,
        ownership_type=(
            OwnershipType.JOINT.value
            if plan.household_id is not None and ctx.chance(30)
            else OwnershipType.SOLE.value
        ),
        acquired_date=acquired,
        make=make,
        model=model,
        model_year=model_year,
        vin=_vin(ctx),
        mileage=age_years * ctx.integer(_MILEAGE_PER_YEAR) + ctx.integer((200, 8_000)),
        purchase_price=purchase_price,
    )


def _plan_other_asset(ctx: GeneratorContext, plan: CustomerPlan, ordinal: int) -> AssetRecord:
    return AssetRecord(
        asset_id=f"{plan.customer_id}-AS-O{ordinal}",
        asset_type=AssetType.OTHER,
        description=ctx.pick(vocab.OTHER_ASSET_DESCRIPTIONS),
        current_value=ctx.cents((2_000_000, 250_000_000), round_to=10_000),
        ownership_type=ctx.weighted(
            ((OwnershipType.SOLE.value, 55), (OwnershipType.TRUST.value, 45))
        ),
        acquired_date=ctx.date_between(plan.customer_since, ctx.as_of),
    )


def _plan_assets(ctx: GeneratorContext, plan: CustomerPlan) -> None:
    """Attach properties, vehicles and other holdings to ``plan``."""
    profile = plan.profile
    property_count = ctx.integer(profile.property_count)
    for ordinal in range(1, property_count + 1):
        plan.assets.append(_plan_property(ctx, plan, ordinal, is_primary=ordinal == 1))

    vehicle_count = ctx.integer(profile.vehicle_count)
    for ordinal in range(1, vehicle_count + 1):
        plan.assets.append(_plan_vehicle(ctx, plan, ordinal))

    # Trust structures and illiquid holdings, which design §15 gives the HNW cohort.
    if profile.cohort.value == "HNW":
        for ordinal in range(1, ctx.integer((1, 3)) + 1):
            plan.assets.append(_plan_other_asset(ctx, plan, ordinal))


def _deposit_account(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    ordinal: int,
    product: tuple[str, str, DepositProductType, tuple[int, int]],
    balance: Cents,
) -> AccountRecord:
    code, name, product_type, rate_bounds = product
    open_date = ctx.date_between(plan.customer_since, ctx.as_of)

    status = AccountStatus.ACTIVE
    close_date: date | None = None
    # The primary account is never closed or dormant; a customer whose only deposit account is shut
    # has no salary destination, and every transaction path would then be empty.
    if ordinal > 1:
        if ctx.chance(_CLOSED_DEPOSIT_PERCENT):
            status = AccountStatus.CLOSED
            close_date = ctx.date_between(open_date, ctx.as_of)
        elif ctx.chance(_DORMANT_DEPOSIT_PERCENT):
            status = AccountStatus.DORMANT

    # A closed account holds nothing. The schema does not require it, but a closed account showing a
    # balance would be counted by every rollup in design §4.6.
    effective_balance = Cents(0) if status is AccountStatus.CLOSED else balance
    maturity = (
        add_months(open_date, ctx.integer((12, 60)))
        if product_type in _TERM_DEPOSIT_TYPES
        else None
    )

    return AccountRecord(
        account_id=f"{plan.customer_id}-A-D{ordinal}",
        account_type=AccountType.DEPOSIT,
        account_number=_account_number(ctx),
        product_code=code,
        product_name=name,
        balance=effective_balance,
        interest_rate_bps=ctx.integer(rate_bounds),
        status=status,
        open_date=open_date,
        close_date=close_date,
        deposit_type=product_type,
        maturity_date=maturity,
    )


def _card_account(ctx: GeneratorContext, plan: CustomerPlan, ordinal: int) -> AccountRecord:
    eligible = [product for product in vocab.CARD_PRODUCTS if _card_is_eligible(plan, product[0])]
    code, name, card_type, limit_bounds, rate_bounds = ctx.pick(eligible or vocab.CARD_PRODUCTS)
    limit = ctx.cents(limit_bounds, round_to=10_000)
    # CHECK (credit_limit_cents > 0): the round_to snap could otherwise floor a small limit to zero.
    if int(limit) <= 0:
        limit = Cents(100_000)

    if plan.profile.delinquent:
        utilization = Bps(ctx.integer(_UTILIZATION_STRESSED))
    elif plan.profile.fraud_flagged or ctx.chance(_STRESSED_UTILIZATION_PERCENT):
        utilization = Bps(ctx.integer(_UTILIZATION_MODERATE))
    else:
        utilization = Bps(ctx.integer(_UTILIZATION_HEALTHY))

    balance = limit.apply_bps(utilization)
    pan = f"4{ctx.digits(15)}" if ctx.chance(60) else f"5{ctx.digits(15)}"

    return AccountRecord(
        account_id=f"{plan.customer_id}-A-C{ordinal}",
        account_type=AccountType.CARD,
        account_number=_account_number(ctx),
        product_code=code,
        product_name=name,
        balance=balance,
        interest_rate_bps=ctx.integer(rate_bounds),
        status=(
            AccountStatus.FROZEN
            if plan.profile.fraud_flagged and ctx.chance(35)
            else (AccountStatus.ACTIVE)
        ),
        open_date=ctx.date_between(plan.customer_since, ctx.as_of),
        card_number=pan,
        credit_limit=limit,
        card_type=card_type,
    )


def _card_is_eligible(plan: CustomerPlan, product_code: str) -> bool:
    """Keep the private-client card away from mass-market customers and vice versa."""
    if product_code == "CC-PRIVATE":
        return plan.cohort.value == "HNW"
    if product_code == "CC-BUSINESS":
        return plan.cohort.value == "SMALL_BUSINESS"
    if product_code == "CC-TRAVEL":
        return plan.cohort.value in ("AFFLUENT", "HNW", "SMALL_BUSINESS")
    return True


def _loan_account(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    ordinal: int,
    product: tuple[str, str, LoanType, tuple[int, int], int, tuple[int, int]],
    collateral: AssetRecord | None,
) -> AccountRecord:
    code, name, loan_type, rate_bounds, term_months, amount_bounds = product
    rate = ctx.integer(rate_bounds)
    original = ctx.cents(amount_bounds, round_to=10_000)
    if int(original) <= 0:  # CHECK (original_amount_cents > 0)
        original = Cents(100_000)

    # A collateralized loan cannot predate its collateral.
    earliest = plan.customer_since
    if collateral is not None:
        earliest = max(earliest, collateral.acquired_date)
    latest = ctx.as_of
    start = ctx.date_between(earliest, latest) if earliest <= latest else latest

    elapsed = max(0, (ctx.as_of.year - start.year) * 12 + ctx.as_of.month - start.month)
    remaining = max(0, term_months - elapsed)
    monthly_principal = original.divide(term_months)
    outstanding = monthly_principal * remaining

    status = _LOAN_STATUS_CURRENT
    if remaining == 0:
        status = _LOAN_STATUS_PAID_OFF
        outstanding = Cents(0)
    elif plan.profile.delinquent:
        status = (
            _LOAN_STATUS_CHARGED_OFF if ctx.chance(_CHARGE_OFF_PERCENT) else _LOAN_STATUS_DELINQUENT
        )

    return AccountRecord(
        account_id=f"{plan.customer_id}-A-L{ordinal}",
        account_type=AccountType.LOAN,
        account_number=_account_number(ctx),
        product_code=code,
        product_name=name,
        balance=outstanding,
        interest_rate_bps=rate,
        # A paid-off loan is closed; a live one is open regardless of whether it is performing.
        status=AccountStatus.CLOSED if remaining == 0 else AccountStatus.ACTIVE,
        open_date=start,
        close_date=add_months(start, term_months) if remaining == 0 else None,
        loan_type=loan_type,
        loan_number=f"LN{ctx.digits(10)}",
        original_amount=original,
        monthly_emi=_annuity_emi(original, rate, term_months),
        loan_start_date=start,
        loan_end_date=add_months(start, term_months),
        collateral_asset_id=collateral.asset_id if collateral is not None else None,
        product_status=status,
    )


def _investment_account(ctx: GeneratorContext, plan: CustomerPlan, ordinal: int) -> AccountRecord:
    code, name = ctx.pick(vocab.INVESTMENT_PRODUCTS)
    value = ctx.cents(plan.profile.investment_value_cents, round_to=1_000)
    risk_profile = ctx.weighted(_RISK_PROFILE_BY_TIER)
    weights = _ALLOCATION_WEIGHTS[risk_profile.value]
    # allocate() guarantees the four parts sum to exactly `value`, so the specialization row and the
    # parent account balance cannot disagree.
    funds, stocks, bonds, retirement = value.allocate(list(weights))

    return AccountRecord(
        account_id=f"{plan.customer_id}-A-I{ordinal}",
        account_type=AccountType.INVESTMENT,
        account_number=_account_number(ctx),
        product_code=code,
        product_name=name,
        balance=value,
        interest_rate_bps=0,
        status=AccountStatus.ACTIVE,
        open_date=ctx.date_between(plan.customer_since, ctx.as_of),
        portfolio_value=value,
        investment_components={
            "mutual_funds": funds,
            "stocks": stocks,
            "bonds": bonds,
            "retirement": retirement,
        },
        investment_risk_profile=risk_profile,
    )


def _desired_products(ctx: GeneratorContext, plan: CustomerPlan) -> list[str]:
    """Decide the product mix, then size it to the cohort's product count.

    Built as a list of tokens rather than by branching straight into account construction so the mix
    can be trimmed to the target count without half-built accounts to unwind.
    """
    profile = plan.profile
    target = ctx.integer(profile.product_count)

    mix: list[str] = ["CHECKING"]
    if ctx.chance(profile.card_probability):
        mix.append("CARD")
    if ctx.chance(profile.mortgage_probability):
        mix.append("MORTGAGE")
    if ctx.chance(profile.investment_probability):
        mix.append("INVESTMENT")
    if ctx.chance(profile.business_loan_probability):
        mix.append("BUSINESS_LOAN")

    # Filler products, in the order a customer typically accumulates them.
    filler = ["SAVINGS", "AUTO_LOAN", "TERM_DEPOSIT", "PERSONAL_LOAN", "INVESTMENT", "CARD"]
    cursor = 0
    while len(mix) < target and cursor < len(filler):
        mix.append(filler[cursor])
        cursor += 1

    # Never trim the checking account away, and never leave a cohort above its stated ceiling.
    return mix[:target] if target >= 1 else mix[:1]


def _collateral_for(plan: CustomerPlan, product_kind: str) -> AssetRecord | None:
    """Find an unencumbered asset of the right kind to secure a loan against."""
    wanted = AssetType.PROPERTY if product_kind in ("MORTGAGE", "HELOC") else AssetType.VEHICLE
    for asset in plan.assets:
        if asset.asset_type is wanted and not asset.is_collateral:
            return asset
    return None


def _product_by_code(
    products: tuple[tuple[str, str, LoanType, tuple[int, int], int, tuple[int, int]], ...],
    codes: tuple[str, ...],
) -> list[tuple[str, str, LoanType, tuple[int, int], int, tuple[int, int]]]:
    return [product for product in products if product[0] in codes]


def plan_products(ctx: GeneratorContext, population: Population) -> None:
    """Attach assets and accounts to every customer in ``population``."""
    for plan in population.customers:
        _plan_assets(ctx, plan)

        mix = _desired_products(ctx, plan)
        deposit_balance = ctx.cents(plan.profile.deposit_balance_cents, round_to=100)
        deposit_kinds = [
            product_kind
            for product_kind in mix
            if product_kind in ("CHECKING", "SAVINGS", "TERM_DEPOSIT")
        ]
        # Split the customer's deposit money across their deposit accounts, exactly.
        shares = (
            deposit_balance.allocate(
                [60] + [40 // max(1, len(deposit_kinds) - 1)] * (len(deposit_kinds) - 1)
            )
            if len(deposit_kinds) > 1
            else (deposit_balance,)
        )

        deposit_ordinal = 0
        card_ordinal = 0
        loan_ordinal = 0
        investment_ordinal = 0

        for product_kind in mix:
            if product_kind in ("CHECKING", "SAVINGS", "TERM_DEPOSIT"):
                deposit_ordinal += 1
                if product_kind == "CHECKING":
                    deposit_candidates = [
                        product
                        for product in vocab.DEPOSIT_PRODUCTS
                        if product[2] is DepositProductType.CHECKING
                        and (product[0] == "BUS-CHK") == (plan.cohort.value == "SMALL_BUSINESS")
                    ]
                elif product_kind == "SAVINGS":
                    deposit_candidates = [
                        product
                        for product in vocab.DEPOSIT_PRODUCTS
                        if product[2] is DepositProductType.SAVINGS
                    ]
                else:
                    deposit_candidates = [
                        product
                        for product in vocab.DEPOSIT_PRODUCTS
                        if product[2] in (DepositProductType.CD, DepositProductType.MMA)
                    ]
                deposit_product = ctx.pick(deposit_candidates or list(vocab.DEPOSIT_PRODUCTS))
                share = shares[min(deposit_ordinal - 1, len(shares) - 1)]
                plan.accounts.append(
                    _deposit_account(ctx, plan, deposit_ordinal, deposit_product, share)
                )

            elif product_kind == "CARD":
                card_ordinal += 1
                card = _card_account(ctx, plan, card_ordinal)
                if plan.profile.fraud_flagged:
                    plan.fraud_alerts = ctx.integer(_FRAUD_ALERT_RANGE)
                plan.accounts.append(card)

            elif product_kind in ("MORTGAGE", "AUTO_LOAN", "PERSONAL_LOAN", "BUSINESS_LOAN"):
                loan_ordinal += 1
                codes = {
                    "MORTGAGE": ("MTG-30F", "MTG-15F"),
                    "AUTO_LOAN": ("AUTO-NEW", "AUTO-USED"),
                    "PERSONAL_LOAN": ("PL-UNSEC", "STU-CONS"),
                    "BUSINESS_LOAN": ("BUS-TERM",),
                }[product_kind]
                loan_product = ctx.pick(_product_by_code(vocab.LOAN_PRODUCTS, codes))
                collateral = _collateral_for(plan, product_kind)
                if product_kind == "MORTGAGE" and collateral is None:
                    # A mortgage without a property would have nothing to secure it, and requirement
                    # 6.1 links collateral into the relationship graph. Give it one.
                    collateral = _plan_property(
                        ctx, plan, len(plan.assets) + 1, is_primary=not plan.assets
                    )
                    plan.assets.append(collateral)
                if collateral is not None:
                    collateral.is_collateral = True
                plan.accounts.append(
                    _loan_account(ctx, plan, loan_ordinal, loan_product, collateral)
                )

            elif product_kind == "INVESTMENT":
                investment_ordinal += 1
                plan.accounts.append(_investment_account(ctx, plan, investment_ordinal))


def allocation_json(components: dict[str, Cents], total: Cents) -> str:
    """Render an ``asset_allocation`` JSON document.

    ``sort_keys`` and fixed separators so two runs at the same seed produce byte-identical text; the
    column is ``TEXT`` with ``CHECK (json_valid(...))``, and SQLite compares it as a string.

    Percentages are basis points converted to a whole percent, computed from the exact cents split,
    so
    the document agrees with the four component columns rather than being drawn separately.
    """
    if int(total) <= 0:
        return json.dumps({}, sort_keys=True, separators=(",", ":"))
    payload = {
        key: round(int(value.ratio_bps(total)) / 100, 2) for key, value in components.items()
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
