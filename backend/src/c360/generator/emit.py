"""Row emission for the identity, product and asset tables.

Everything here is a mechanical translation from the plan built by :mod:`c360.generator.people`,
:mod:`c360.generator.products` and :mod:`c360.generator.milestones` into the positional tuples
:mod:`c360.generator.tables` declares. The decisions were all made during planning; this module's
only
job is to write them in a shape the schema accepts.

The parts that are not mechanical are the schema's paired constraints, and they are called out at
each
site:

* ``account``: ``CHECK ((account_status = 'CLOSED') = (close_date IS NOT NULL))``. The plan already
  keeps the two in step, and the assertion here is a second line of defence — a mismatch is a
  generator bug, and finding it as a ``RowArityError``-style failure at emission is far cheaper than
  finding it as an ``IntegrityError`` after 20,000 transactions have been staged.
* ``credit_card``: ``CHECK (card_last4 = substr(card_number, -4))``. The last-4 is *derived* here,
  never drawn, so the two cannot disagree.
* ``contact_info``: a customer with no contact details gets **no row**, rather than a row of NULLs.
  Requirement 4.7 distinguishes absent data from empty data, and the thin-file cohort is where that
  distinction is exercised.

``household_deposits_cents`` on ``deposit`` is a rollup across the household's members, so it is
computed once up front rather than per account: the alternative is a scan of every member's accounts
for
every deposit account they hold.
"""

from __future__ import annotations

from typing import Final

from c360.domain.enums import AccountStatus, AccountType, AssetType
from c360.domain.money import Bps, Cents
from c360.generator.context import (
    SOURCE_CARDS,
    SOURCE_CORE_BANKING,
    SOURCE_CRM,
    SOURCE_LOS,
    SOURCE_MDM,
    SOURCE_WEALTH,
    GeneratorContext,
)
from c360.generator.plan import AccountRecord, CustomerPlan, Population
from c360.generator.products import allocation_json
from c360.generator.tables import (
    ACCOUNT,
    ASSET,
    CONTACT_INFO,
    CREDIT_CARD,
    CUSTOMER,
    DEPOSIT,
    EMPLOYER,
    HOUSEHOLD,
    INVESTMENT,
    LOAN,
    PROPERTY,
    VEHICLE,
    Dataset,
)

#: Rewards accrual as a share of the card balance. A card that gets used has earned something; the
#: exact rate is cosmetic, which is why it is one constant rather than a per-product field.
_REWARDS_RATE_BPS: Final = Bps(150)


class EmissionError(RuntimeError):
    """Raised when a planned record cannot satisfy a schema constraint.

    Distinct from an ``IntegrityError`` on purpose: it fires before the load, names the customer and
    the
    constraint, and means a generator invariant was broken rather than that the database rejected
    otherwise-valid data.
    """


def _source_for_account(account: AccountRecord) -> str:
    """Attribute an account to the system that would really own it.

    Requirement 14.6 carries ``source_system`` through to reads, and a platform where every row
    claims
    to come from ``CORE_BANKING`` makes the field decorative.
    """
    if account.account_type is AccountType.CARD:
        return SOURCE_CARDS
    if account.account_type is AccountType.INVESTMENT:
        return SOURCE_WEALTH
    if account.account_type is AccountType.LOAN:
        return SOURCE_LOS
    return SOURCE_CORE_BANKING


def _household_deposit_totals(population: Population) -> dict[str, Cents]:
    """Total open deposit balances per household, for ``deposit.household_deposits_cents``."""
    totals: dict[str, Cents] = {}
    for plan in population.customers:
        if plan.household_id is None:
            continue
        running = totals.get(plan.household_id, Cents(0))
        for account in plan.accounts_of(AccountType.DEPOSIT):
            if account.status is not AccountStatus.CLOSED:
                running += account.balance
        totals[plan.household_id] = running
    return totals


def _emit_employers(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    del ctx  # employers carry no as-of date
    for row in population.employers:
        dataset.add(EMPLOYER, row)


def _emit_households(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    for household in population.households:
        dataset.add(
            HOUSEHOLD,
            (
                household.household_id,
                household.name,
                household.primary_customer_id,
                household.address_hash,
                household.member_count,
                ctx.as_of_iso,
            ),
        )


def _emit_customer(ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset) -> None:
    dataset.add(
        CUSTOMER,
        (
            plan.customer_id,
            plan.customer_name,
            plan.customer_type.value,
            plan.segment.value,
            plan.customer_since.isoformat(),
            plan.date_of_birth.isoformat() if plan.date_of_birth is not None else None,
            plan.citizenship,
            plan.occupation,
            plan.employer_id,
            plan.employment_status,
            plan.marital_status,
            plan.value_tier.value,
            plan.value_score,
            plan.language,
            plan.preferred_channel,
            plan.household_id,
            ctx.as_of_iso,
            SOURCE_MDM if plan.household_id is not None else SOURCE_CRM,
        ),
    )


def _emit_contact(ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset) -> None:
    """Emit contact details, or nothing at all when the customer has none.

    See the module docstring: no row is a different fact from a row of NULLs, and requirement 4.7
    depends on the difference.
    """
    if plan.email is None and plan.phone_number is None and plan.mobile_number is None:
        return
    dataset.add(
        CONTACT_INFO,
        (
            plan.customer_id,
            plan.email,
            plan.phone_number,
            plan.mobile_number,
            plan.street_address,
            plan.unit,
            plan.city,
            plan.state,
            "US",
            plan.postal_code,
            ctx.as_of_iso,
        ),
    )


def _emit_account(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    account: AccountRecord,
    dataset: Dataset,
    household_deposits: dict[str, Cents],
) -> None:
    is_closed = account.status is AccountStatus.CLOSED
    if is_closed != (account.close_date is not None):
        raise EmissionError(
            f"{plan.customer_id}/{account.account_id}: account_status={account.status.value} "
            f"but close_date={account.close_date!r}; the schema requires them to agree"
        )

    # A card's available balance is headroom against the limit; a deposit's is the balance itself.
    if account.account_type is AccountType.CARD and account.credit_limit is not None:
        available = account.credit_limit - account.balance
    elif account.account_type is AccountType.DEPOSIT:
        available = account.balance
    else:
        available = None

    dataset.add(
        ACCOUNT,
        (
            account.account_id,
            plan.customer_id,
            account.account_number,
            account.account_type.value,
            account.product_name,
            account.product_code,
            int(account.balance),
            int(available) if available is not None else None,
            account.interest_rate_bps,
            account.status.value,
            account.open_date.isoformat(),
            account.close_date.isoformat() if account.close_date is not None else None,
            ctx.as_of_iso,
        ),
    )

    if account.account_type is AccountType.DEPOSIT:
        dataset.add(
            DEPOSIT,
            (
                account.account_id,
                account.deposit_type.value if account.deposit_type is not None else None,
                (
                    int(household_deposits.get(plan.household_id or "", Cents(0)))
                    if plan.household_id is not None
                    else None
                ),
                account.maturity_date.isoformat() if account.maturity_date is not None else None,
            ),
        )

    elif account.account_type is AccountType.LOAN:
        if account.original_amount is None or int(account.original_amount) <= 0:
            raise EmissionError(
                f"{plan.customer_id}/{account.account_id}: loan requires a positive "
                f"original_amount_cents, got {account.original_amount!r}"
            )
        dataset.add(
            LOAN,
            (
                account.account_id,
                account.loan_number,
                account.loan_type.value if account.loan_type is not None else None,
                int(account.original_amount),
                int(account.monthly_emi) if account.monthly_emi is not None else None,
                account.product_status,
                (
                    account.loan_start_date.isoformat()
                    if account.loan_start_date is not None
                    else None
                ),
                account.loan_end_date.isoformat() if account.loan_end_date is not None else None,
                account.collateral_asset_id,
            ),
        )

    elif account.account_type is AccountType.CARD:
        if account.card_number is None or account.credit_limit is None:
            raise EmissionError(
                f"{plan.customer_id}/{account.account_id}: card requires a PAN and a credit limit"
            )
        limit = account.credit_limit
        dataset.add(
            CREDIT_CARD,
            (
                account.account_id,
                account.card_number,
                # Derived, never drawn: CHECK (card_last4 = substr(card_number, -4)).
                account.card_number[-4:],
                account.card_type,
                int(limit),
                # Truncating division, matching the SQL expression in design §4.6 so the Phase 3.1
                # stored-equals-computed assertion holds.
                int(account.balance.ratio_bps(limit)) if int(limit) > 0 else None,
                int(account.balance.apply_bps(_REWARDS_RATE_BPS)),
                int(account.balance.divide(3)),
                ctx.integer((0, 3)) if plan.profile.delinquent else 0,
                plan.fraud_alerts if plan.profile.fraud_flagged else 0,
            ),
        )

    elif account.account_type is AccountType.INVESTMENT:
        components = account.investment_components
        value = account.portfolio_value or Cents(0)
        dataset.add(
            INVESTMENT,
            (
                account.account_id,
                int(value),
                allocation_json(components, value),
                int(components.get("mutual_funds", Cents(0))),
                int(components.get("stocks", Cents(0))),
                int(components.get("bonds", Cents(0))),
                int(components.get("retirement", Cents(0))),
                (
                    account.investment_risk_profile.value
                    if account.investment_risk_profile is not None
                    else None
                ),
            ),
        )


def _emit_assets(ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset) -> None:
    for asset in plan.assets:
        dataset.add(
            ASSET,
            (
                asset.asset_id,
                plan.customer_id,
                asset.asset_type.value,
                asset.description,
                int(asset.current_value),
                asset.ownership_type,
                asset.acquired_date.isoformat(),
                1 if asset.is_collateral else 0,
                ctx.as_of_iso,
                SOURCE_MDM,
            ),
        )

        if asset.asset_type is AssetType.PROPERTY:
            dataset.add(
                PROPERTY,
                (
                    asset.asset_id,
                    asset.property_type,
                    asset.address_line1,
                    asset.city,
                    asset.state,
                    asset.postal_code,
                    int(asset.purchase_price) if asset.purchase_price is not None else None,
                    int(asset.assessed_value) if asset.assessed_value is not None else None,
                    asset.square_feet,
                    asset.year_built,
                ),
            )
        elif asset.asset_type is AssetType.VEHICLE:
            dataset.add(
                VEHICLE,
                (
                    asset.asset_id,
                    asset.make,
                    asset.model,
                    asset.model_year,
                    asset.vin,
                    asset.mileage,
                    int(asset.purchase_price) if asset.purchase_price is not None else None,
                ),
            )


def emit_core(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    """Emit employers, households, customers, contacts, accounts, specializations and assets."""
    _emit_employers(ctx, population, dataset)
    _emit_households(ctx, population, dataset)

    household_deposits = _household_deposit_totals(population)
    for plan in population.customers:
        _emit_customer(ctx, plan, dataset)
        _emit_contact(ctx, plan, dataset)
        _emit_assets(ctx, plan, dataset)
        for account in plan.accounts:
            _emit_account(ctx, plan, account, dataset, household_deposits)
