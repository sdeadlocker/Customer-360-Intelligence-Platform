"""Transaction generation (task 2.4).

Design §15 asks for "category-weighted monthly budgets with seasonality and deliberate anomalies, so
expense analytics and the Financial Health Agent have something real to detect", and ~200
transactions per customer over 24 months.

Budgets first, amounts second
-----------------------------

The obvious implementation draws an amount per transaction from a per-category range. It produces a
monthly total that is the transaction count times a constant, which means the trend line in
requirement 5.9 is flat noise, savings rate is unrelated to income, and a deviation detector has
nothing to find except the count.

This module inverts that. Each month starts from a **budget**: a share of the customer's income set
by
their cohort. The budget is then split across the month's transactions with
:meth:`c360.domain.money.Cents.allocate`, weighted by category. Two things follow. The monthly total
is
a real economic quantity rather than an emergent one, so the savings rate the Phase 3.2 health score
computes is meaningful. And the split is exact — ``allocate`` uses the largest-remainder method, so
the
transactions sum to the budget to the cent, with no rounding residue to explain.

Signs follow :mod:`c360.domain.money`: a debit is negative, a credit is positive.

Seasonality and anomalies are different things
----------------------------------------------

Seasonality is a *predictable* multiplier — December shopping, July travel — applied per category
from
:data:`c360.generator.vocab.SEASONALITY_PERCENT`. A deviation detector should learn it and not alarm
on
it. Anomalies are injected separately, into specific months recorded on the plan, and those are what
requirement 7.4 and the Phase 3 deviation flag are supposed to catch. Keeping them distinct is what
makes it possible to assert that the detector finds the anomalies *and* ignores the seasonality.

Life-event corroboration
------------------------

Every planned life event that falls inside the window gets its evidence emitted here — a down
payment
for a home purchase, a step change in salary for a promotion, pension credits replacing salary at
retirement — and the transaction IDs are appended to the event's ``signals``. That is what makes
requirement 7.3's citation resolvable and design §14.3's life-event scoring measurable, rather than
a
label with nothing behind it.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from c360.domain.enums import AccountStatus, AccountType, LifeEventType
from c360.domain.fees import FeeSchedule, ProductFeeRule, default_schedule_path
from c360.domain.money import Bps, Cents
from c360.generator import vocab
from c360.generator.adversarial import adversarial_merchant
from c360.generator.context import GeneratorContext
from c360.generator.fees import (
    FeeCoverage,
    emit_fee_cycle,
    emit_wire_activity,
    plan_account_fees,
)
from c360.generator.plan import AccountRecord, CustomerPlan, PlannedLifeEvent, Population
from c360.generator.tables import TXN, Dataset

#: Share of monthly income that leaves as spend, in percent, per cohort. The delinquent cohort
#: spends
#: more than it earns, which is the point of the cohort: it is what makes the savings rate negative
#: and
#: the health score in Phase 3.2 band poorly for a defensible reason.
_SPEND_RATE_PERCENT: Final[dict[str, tuple[int, int]]] = {
    "MASS_MARKET": (58, 78),
    "AFFLUENT": (42, 62),
    "HNW": (22, 42),
    "SMALL_BUSINESS": (55, 80),
    "THIN_FILE": (72, 92),
    "DELINQUENT": (88, 112),
    "FRAUD_FLAGGED": (60, 85),
    "ISOLATED": (50, 72),
}

#: Percent of customers who get a deliberate spend anomaly.
_ANOMALY_CUSTOMER_PERCENT: Final = 34

#: Anomalous months per selected customer.
_ANOMALY_MONTH_COUNT: Final = (1, 2)

#: Budget multiplier for an anomalous month, in percent.
_ANOMALY_MULTIPLIER_PERCENT: Final = (240, 520)

#: Share of discretionary transactions posted to a credit card rather than the deposit account.
_CARD_SPEND_PERCENT: Final = 62

#: Pension as a share of pre-retirement income, in percent.
_PENSION_PERCENT: Final = (42, 68)

#: Salary step at a promotion or job change, in percent.
_PROMOTION_STEP_PERCENT: Final = (7, 22)
_JOB_CHANGE_STEP_PERCENT: Final = (4, 28)

#: Percent of transactions in the most recent month left ``PENDING`` rather than ``POSTED``.
_PENDING_PERCENT: Final = 18

#: Percent of the fraud cohort's card transactions that failed.
_FAILED_PERCENT: Final = 12

#: Employment statuses that receive no salary credit.
_NO_SALARY_STATUSES: Final = ("UNEMPLOYED", "STUDENT")

#: Merchant settlements a small-business customer receives per month, instead of one payroll credit.
_BUSINESS_SETTLEMENTS_PER_MONTH: Final = 3

#: Percent of a delinquent customer's loan payments that fail. This is the mechanism behind their
#: arrears: the DPD bucket on the risk profile and the failed debits here describe the same fact.
_MISSED_PAYMENT_PERCENT: Final = 38

#: Baseline monthly income the per-transaction ranges in :data:`vocab.SPEND_CATEGORIES` are written
#: against. Used only to scale one-off corroborating amounts, never the budgeted ones.
_BASELINE_INCOME_CENTS: Final = 500_000

#: Bounds on the income scaling factor, in basis points, so a UHNW customer's coffee is expensive
#: but
#: not absurd and a thin-file customer's is not free.
_SCALE_FLOOR_BPS: Final = 4_000
_SCALE_CEILING_BPS: Final = 90_000

_MONTHS_IN_YEAR: Final = 12


#: One-off corroborating transactions per life-event type, as
#: ``(category, cents bounds, is_credit)``. A table rather than an ``if``/``elif`` chain, so
#: the evidence model for all twelve event types is reviewable in one place, and so adding an event
#: type is a data change rather than a control-flow change.
_ONE_OFF_EVIDENCE: Final[dict[LifeEventType, tuple[tuple[str, tuple[int, int], bool], ...]]] = {
    LifeEventType.HOME_PURCHASE: (("HOME_PURCHASE", (2_500_000, 12_000_000), False),),
    LifeEventType.CHILD_BIRTH: (("HEALTHCARE", (180_000, 900_000), False),),
    LifeEventType.MARRIAGE: (
        ("ENTERTAINMENT", (600_000, 4_500_000), False),
        ("SHOPPING", (150_000, 900_000), False),
    ),
    LifeEventType.RELOCATION: (("HOME_IMPROVEMENT", (180_000, 1_200_000), False),),
    LifeEventType.BUSINESS_START: (("PROFESSIONAL_SERVICES", (90_000, 600_000), False),),
    LifeEventType.INHERITANCE: (("TRANSFER", (5_000_000, 90_000_000), True),),
    LifeEventType.BEREAVEMENT: (("PROFESSIONAL_SERVICES", (150_000, 800_000), False),),
    # Retirement's real signal is the salary-to-pension change; a lump-sum drawdown makes it
    # unambiguous.
    LifeEventType.RETIREMENT: (("TRANSFER", (1_000_000, 20_000_000), True),),
}

#: Recurring corroborating transactions, as ``(category, cents bounds, months)``. Design §15's
#: "a vehicle purchase gets a down-payment debit and an insurance recurring payment" is the pattern:
#: a one-off establishes the event, the recurring series establishes that it stuck.
_RECURRING_EVIDENCE: Final[dict[LifeEventType, tuple[tuple[str, tuple[int, int], int], ...]]] = {
    LifeEventType.HOME_PURCHASE: (("INSURANCE", (9_000, 34_000), 12),),
    LifeEventType.CHILD_BIRTH: (("CHILDCARE", (35_000, 160_000), 12),),
    LifeEventType.RELOCATION: (("UTILITIES", (9_000, 30_000), 6),),
    LifeEventType.EDUCATION: (("EDUCATION", (120_000, 700_000), 8),),
    LifeEventType.BUSINESS_START: (("BUSINESS_REVENUE", (200_000, 2_000_000), 10),),
    LifeEventType.DIVORCE: (("PROFESSIONAL_SERVICES", (60_000, 380_000), 5),),
}

#: Events whose evidence is the salary credit changing, not a new debit.
_SALARY_EVIDENCED_EVENTS: Final = (LifeEventType.JOB_CHANGE, LifeEventType.PROMOTION)


class _TxnWriter:
    """Sequential transaction-ID allocator and row sink for one customer.

    A small class rather than a closure because the sequence number has to be shared across a dozen
    emitting helpers, and threading a mutable counter through each of them by hand is how two
    transactions end up with the same ID.
    """

    __slots__ = ("_dataset", "_plan", "_sequence")

    def __init__(self, dataset: Dataset, plan: CustomerPlan) -> None:
        self._dataset = dataset
        self._plan = plan
        self._sequence = 0

    def write(
        self,
        *,
        account: AccountRecord,
        when: date,
        amount: Cents,
        transaction_type: str,
        category: str,
        merchant: str,
        channel: str,
        status: str,
    ) -> str:
        """Emit one transaction and return its ID, so a life event can cite it."""
        self._sequence += 1
        transaction_id = f"{self._plan.customer_id}-T-{self._sequence:05d}"
        self._dataset.add(
            TXN,
            (
                transaction_id,
                account.account_id,
                self._plan.customer_id,
                when.isoformat(),
                int(amount),
                transaction_type,
                category,
                merchant,
                channel,
                status,
            ),
        )
        return transaction_id


def _relationship_balance(plan: CustomerPlan) -> Cents:
    """Deposits plus investments — the "relationship balance" a premier waiver is tested against.

    Defined by ``pol-deposit-account`` as the sum of eligible deposit and investment balances, and
    computed here exactly as :mod:`c360.generator.profiles` computes ``total_deposits_cents`` and
    ``total_investments_cents`` (closed accounts excluded, balances as-is). The detector reads those
    two derived columns, so keeping the arithmetic identical is what stops the generator from
    billing a cycle the detector believes was waived — a divergence there would manufacture false
    findings.
    """
    total = Cents(0)
    for account in plan.accounts:
        if account.status is AccountStatus.CLOSED:
            continue
        if account.account_type in (AccountType.DEPOSIT, AccountType.INVESTMENT):
            total += account.balance
    return total


def _income_scale_bps(plan: CustomerPlan) -> Bps:
    """Income relative to the baseline the category ranges were written against, in basis points."""
    raw = int(plan.monthly_income) * 10_000 // _BASELINE_INCOME_CENTS
    return Bps(max(_SCALE_FLOOR_BPS, min(_SCALE_CEILING_BPS, raw)))


def _merchant_for(ctx: GeneratorContext, category: str) -> str:
    options = vocab.MERCHANTS.get(category)
    if options:
        return ctx.pick(options)
    return category.replace("_", " ").title()


def _seasonality(category: str, month: int) -> Bps:
    """Seasonal multiplier for ``category`` in calendar ``month`` (1-12), as basis points."""
    curve = vocab.SEASONALITY_PERCENT.get(category)
    if curve is None:
        return Bps(10_000)
    return Bps(curve[month - 1] * 100)


def _status_for(
    ctx: GeneratorContext, when: date, months: list[date], *, on_card: bool, fraud: bool
) -> str:
    """Decide the settlement status.

    Only the most recent month carries pending transactions, because a pending transaction from
    eighteen months ago is a data-quality defect rather than variety.
    """
    if fraud and on_card and ctx.chance(_FAILED_PERCENT):
        return vocab.TXN_STATUS_FAILED
    if when >= months[-1] and ctx.chance(_PENDING_PERCENT):
        return vocab.TXN_STATUS_PENDING
    return vocab.TXN_STATUS_POSTED


def _salary_schedule(ctx: GeneratorContext, plan: CustomerPlan, months: list[date]) -> list[Cents]:
    """Monthly gross income across the window, ending at the customer's current income.

    Built backwards from the present. ``plan.monthly_income`` is the *current* figure, so a
    promotion
    eighteen months ago means income before it was lower — not that income after it was higher than
    what the profile says. Getting this the wrong way round makes every customer's income drift
    upward
    away from the value the financial profile reports.
    """
    schedule = [plan.monthly_income] * len(months)
    if not plan.life_events:
        return schedule

    for event in sorted(plan.life_events, key=lambda item: item.event_date, reverse=True):
        index = _month_index(event.event_date, months)
        if index is None:
            continue
        if event.life_event_type is LifeEventType.PROMOTION:
            step = Bps((100 - ctx.integer(_PROMOTION_STEP_PERCENT)) * 100)
            for i in range(index):
                schedule[i] = schedule[i].apply_bps(step)
        elif event.life_event_type is LifeEventType.JOB_CHANGE:
            step = Bps((100 - ctx.integer(_JOB_CHANGE_STEP_PERCENT)) * 100)
            for i in range(index):
                schedule[i] = schedule[i].apply_bps(step)
        elif event.life_event_type is LifeEventType.RETIREMENT:
            pension = Bps(ctx.integer(_PENSION_PERCENT) * 100)
            for i in range(index, len(months)):
                schedule[i] = schedule[i].apply_bps(pension)
    return schedule


def _month_index(when: date, months: list[date]) -> int | None:
    """Position of ``when``'s month in the window, or ``None`` if it falls outside."""
    for index, start in enumerate(months):
        if start.year == when.year and start.month == when.month:
            return index
    return None


def _pick_recurring(ctx: GeneratorContext) -> list[tuple[str, str, Cents]]:
    """Fix this customer's recurring debits: same merchant and near-same amount every month.

    Stable merchant and amount is the whole point — requirement 7.4 and the Phase 3 deviation flag
    both
    depend on recurring commitments being recognizable as recurring. Redrawing the merchant monthly
    would make every subscription look like a new one.
    """
    chosen: list[tuple[str, str, Cents]] = []
    for category, bounds in vocab.RECURRING_DEBITS:
        chosen.append((category, _merchant_for(ctx, category), ctx.cents(bounds, round_to=50)))
    return chosen


def _emit_discretionary(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    writer: _TxnWriter,
    *,
    month_start: date,
    budget: Cents,
    count: int,
    deposit: AccountRecord | None,
    cards: list[AccountRecord],
    months: list[date],
) -> None:
    """Split ``budget`` across ``count`` transactions and emit them."""
    if count < 1 or int(budget) <= 0:
        return

    categories: list[str] = []
    weights: list[int] = []
    category_options = [(name, weight) for name, weight, _ in vocab.SPEND_CATEGORIES]
    for _ in range(count):
        category = ctx.weighted(category_options)
        categories.append(category)
        seasonal = int(_seasonality(category, month_start.month))
        # Weight combines the category's budget share, its seasonal multiplier and per-transaction
        # variation, so allocate() produces a spread of amounts rather than `count` equal ones.
        base = next(weight for name, weight, _ in vocab.SPEND_CATEGORIES if name == category)
        weights.append(max(1, base * seasonal // 10_000 * ctx.integer((5, 24))))

    shares = budget.allocate(weights)
    for category, share in zip(categories, shares, strict=True):
        if int(share) <= 0:
            continue
        on_card = bool(cards) and ctx.chance(_CARD_SPEND_PERCENT)
        account = ctx.pick(cards) if on_card else deposit
        if account is None:
            continue
        channel = "CARD" if on_card else ctx.weighted(vocab.TXN_CHANNELS)
        if category == "ATM_WITHDRAWAL":
            channel = "ATM"
        writer.write(
            account=account,
            when=ctx.day_in_month(month_start),
            amount=-share,
            transaction_type=vocab.TXN_TYPE_DEBIT,
            category=category,
            merchant=_merchant_for(ctx, category),
            channel=channel,
            status=_status_for(
                ctx, month_start, months, on_card=on_card, fraud=plan.profile.fraud_flagged
            ),
        )


def _emit_income(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    writer: _TxnWriter,
    *,
    deposit: AccountRecord | None,
    month_start: date,
    gross: Cents,
) -> int:
    """Emit the month's income credits. Returns how many transactions were written.

    A small-business customer's revenue arrives as several merchant settlements rather than one
    payroll credit, which matters because requirement 5.9's category aggregation should show
    ``BUSINESS_REVENUE`` as a stream of receipts, not one monthly figure that looks like a wage.
    """
    if deposit is None or plan.employment_status in _NO_SALARY_STATUSES or int(gross) <= 0:
        return 0

    if plan.cohort.value == "SMALL_BUSINESS":
        written = 0
        parts = gross.allocate(
            [ctx.integer((2, 9)) for _ in range(_BUSINESS_SETTLEMENTS_PER_MONTH)]
        )
        for part in parts:
            if int(part) <= 0:
                continue
            written += 1
            writer.write(
                account=deposit,
                when=ctx.day_in_month(month_start, low=1, high=27),
                amount=part,
                transaction_type=vocab.TXN_TYPE_CREDIT,
                category="BUSINESS_REVENUE",
                merchant=_merchant_for(ctx, "BUSINESS_REVENUE"),
                channel="ACH",
                status=vocab.TXN_STATUS_POSTED,
            )
        return written

    writer.write(
        account=deposit,
        when=ctx.day_in_month(month_start, low=25, high=28),
        amount=gross,
        transaction_type=vocab.TXN_TYPE_CREDIT,
        category="SALARY",
        merchant=_merchant_for(ctx, "SALARY"),
        channel="ACH",
        status=vocab.TXN_STATUS_POSTED,
    )
    return 1


def _emit_commitments(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    writer: _TxnWriter,
    *,
    deposit: AccountRecord | None,
    month_start: date,
    recurring: list[tuple[str, str, Cents]],
    active_loans: list[AccountRecord],
) -> tuple[int, Cents]:
    """Emit the month's recurring debits and loan EMIs.

    Returns ``(transaction count, total outflow)``. The outflow feeds
    ``financial_profile.monthly_expense_cents``, so it must be the sum of what was actually written
    rather than an estimate.
    """
    if deposit is None:
        return 0, Cents(0)

    written = 0
    outflow = Cents(0)

    for category, merchant, base_amount in recurring:
        amount = base_amount.apply_bps(_seasonality(category, month_start.month))
        if int(amount) <= 0:
            continue
        written += 1
        outflow += amount
        writer.write(
            account=deposit,
            when=ctx.day_in_month(month_start, low=1, high=14),
            amount=-amount,
            transaction_type=vocab.TXN_TYPE_DEBIT,
            category=category,
            merchant=merchant,
            channel="ACH",
            status=vocab.TXN_STATUS_POSTED,
        )

    for loan in active_loans:
        emi = loan.monthly_emi
        start = loan.loan_start_date or loan.open_date
        if emi is None or int(emi) <= 0:
            continue
        # No EMI before the loan was drawn down.
        if start.replace(day=1) > month_start:
            continue
        written += 1
        outflow += emi
        writer.write(
            account=deposit,
            when=ctx.day_in_month(month_start, low=2, high=12),
            amount=-emi,
            transaction_type=vocab.TXN_TYPE_DEBIT,
            category="LOAN_PAYMENT",
            merchant=_merchant_for(ctx, "LOAN_PAYMENT"),
            channel="ACH",
            # A delinquent customer misses payments; that is what puts them in arrears.
            status=(
                vocab.TXN_STATUS_FAILED
                if plan.profile.delinquent and ctx.chance(_MISSED_PAYMENT_PERCENT)
                else vocab.TXN_STATUS_POSTED
            ),
        )

    return written, outflow


def _emit_corroboration(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    writer: _TxnWriter,
    event: PlannedLifeEvent,
    *,
    deposit: AccountRecord | None,
    months: list[date],
) -> None:
    """Emit the transactions that make ``event`` inferable, recording their IDs as its signals.

    Design §15 names the pattern: "a vehicle purchase gets a down-payment debit and an insurance
    recurring payment". Each branch below is the equivalent for its event type. The IDs land in
    ``event.signals``, which requirement 7.3 renders as the citation for an inferred event.
    """
    if deposit is None:
        return
    when = event.event_date
    # Only events inside the window get evidence, and this guard is what enforces the rule
    # :mod:`c360.generator.milestones` states: an event is inferable only if it is corroborated.
    #
    # Without it, a HOME_PURCHASE anchored to a 30-year mortgage's start date emits a down payment
    # dated 30 years ago. That transaction is outside the 24-month history the whole platform
    # assumes:
    # it would fall outside every expense-analytics range, skew `min(transaction_date)`, and put a
    # row
    # in `txn` that no monthly trend query would ever surface. Out-of-window events are recorded as
    # SYSTEM_OF_RECORD or CUSTOMER_DECLARED, which need no signals.
    if when < ctx.history_start:
        return
    scale = _income_scale_bps(plan)

    def one_off(category: str, bounds: tuple[int, int], *, credit: bool = False) -> None:
        amount = ctx.cents(bounds, round_to=100).apply_bps(scale)
        if int(amount) <= 0:
            return
        signal = writer.write(
            account=deposit,
            when=ctx.jitter_days(when, 9),
            amount=amount if credit else -amount,
            transaction_type=vocab.TXN_TYPE_CREDIT if credit else vocab.TXN_TYPE_DEBIT,
            category=category,
            merchant=_merchant_for(ctx, category),
            channel="WIRE" if credit else ctx.weighted(vocab.TXN_CHANNELS),
            status=vocab.TXN_STATUS_POSTED,
        )
        event.signals.append(signal)

    def recurring(category: str, bounds: tuple[int, int], count: int) -> None:
        start = _month_index(when, months)
        if start is None:
            return
        amount = ctx.cents(bounds, round_to=50).apply_bps(scale)
        merchant = _merchant_for(ctx, category)
        for index in range(start, min(start + count, len(months))):
            signal = writer.write(
                account=deposit,
                when=ctx.day_in_month(months[index], low=2, high=26),
                amount=-amount,
                transaction_type=vocab.TXN_TYPE_DEBIT,
                category=category,
                merchant=merchant,
                channel="ACH",
                status=vocab.TXN_STATUS_POSTED,
            )
            event.signals.append(signal)

    event_type = event.life_event_type
    for category, bounds, is_credit in _ONE_OFF_EVIDENCE.get(event_type, ()):
        one_off(category, bounds, credit=is_credit)
    for category, bounds, count in _RECURRING_EVIDENCE.get(event_type, ()):
        recurring(category, bounds, count)

    if event_type in _SALARY_EVIDENCED_EVENTS:
        # Corroborated by the step change in the salary credit itself, which
        # :func:`_salary_schedule` already applied. Cite the month's salary rather than inventing
        # a debit that would not exist.
        index = _month_index(when, months)
        if index is not None:
            event.signals.append(f"{plan.customer_id}-SALARY-{months[index]:%Y-%m}")


def generate_transactions(
    ctx: GeneratorContext,
    population: Population,
    dataset: Dataset,
    *,
    fee_schedule: FeeSchedule | None = None,
) -> None:
    """Emit every customer's transaction history and record the derived monthly expense.

    Writes ``txn`` rows straight into ``dataset`` rather than staging them on the plan: at ~200 per
    customer this is the only table where holding a second copy in memory would be worth avoiding,
    and
    nothing downstream needs to read individual transactions back.

    ``fee_schedule`` supplies the deposit pricing that :mod:`c360.generator.fees` bills against,
    defaulting to the committed schedule. A schedule that cannot be read raises rather than
    degrading to "no fees": a seeded dataset silently missing its fee postings would make the
    fee-recovery play report no leakage, which reads as good news instead of as a broken seed.
    """
    months = ctx.history_months()
    schedule = fee_schedule or FeeSchedule.from_file(default_schedule_path())
    fee_rules: dict[str, ProductFeeRule] = dict(schedule.products)
    # Population-level floor: guarantees each planted fee defect occurs at least once, however small
    # `--count` is, so no detector is left with nothing to find (see generator.fees.FeeCoverage).
    fee_coverage = FeeCoverage.create()

    for plan in population.customers:
        writer = _TxnWriter(dataset, plan)
        deposit = plan.primary_deposit
        cards = [
            account
            for account in plan.accounts_of(AccountType.CARD)
            if account.status is AccountStatus.ACTIVE
        ]
        active_loans = [
            account
            for account in plan.accounts_of(AccountType.LOAN)
            if account.status is AccountStatus.ACTIVE and account.monthly_emi is not None
        ]

        salary = _salary_schedule(ctx, plan, months)
        recurring = _pick_recurring(ctx)
        spend_low, spend_high = _SPEND_RATE_PERCENT[plan.cohort.value]
        spend_rate = Bps(ctx.integer((spend_low, spend_high)) * 100)

        # Fee behaviour is settled per account before the month loop, because leakage is a property
        # of a mispriced account that persists, not a per-cycle coin flip (see generator.fees).
        relationship_balance = _relationship_balance(plan)
        fee_plans = plan_account_fees(
            ctx,
            plan,
            fee_rules,
            relationship_balance_cents=relationship_balance,
            coverage=fee_coverage,
        )

        anomaly_indices: list[int] = []
        if ctx.chance(_ANOMALY_CUSTOMER_PERCENT):
            anomaly_indices = sorted(
                ctx.sample(range(len(months)), ctx.integer(_ANOMALY_MONTH_COUNT))
            )
            plan.anomaly_months = [f"{months[i]:%Y-%m}" for i in anomaly_indices]

        monthly_total = ctx.integer(plan.profile.txn_per_month)
        expense_total = Cents(0)
        expense_months = 0

        for index, month_start in enumerate(months):
            fixed_count = _emit_income(
                ctx, plan, writer, deposit=deposit, month_start=month_start, gross=salary[index]
            )
            commitment_count, committed = _emit_commitments(
                ctx,
                plan,
                writer,
                deposit=deposit,
                month_start=month_start,
                recurring=recurring,
                active_loans=active_loans,
            )
            fixed_count += commitment_count
            expense_total += committed

            # ---------------------------------------------------------------- discretionary budget
            base_budget = plan.monthly_income.apply_bps(spend_rate)
            budget = base_budget
            if index in anomaly_indices:
                budget = budget.apply_bps(Bps(ctx.integer(_ANOMALY_MULTIPLIER_PERCENT) * 100))

            discretionary_count = max(2, monthly_total - fixed_count)
            _emit_discretionary(
                ctx,
                plan,
                writer,
                month_start=month_start,
                budget=budget,
                count=discretionary_count,
                deposit=deposit,
                cards=cards,
                months=months,
            )
            # The *base* budget, not the anomalous one. `financial_profile.monthly_expense_cents` is
            # the customer's typical outflow, and folding a deliberate 5x spike into it would hide
            # the
            # very anomaly the spike was injected to expose.
            expense_total += base_budget
            expense_months += 1

            # ---------------------------------------------------------------- fees (Phase 22)
            # Emitted last in the cycle so the month's qualifying credit is already decided: the
            # waiver test reads it, and `salary[index]` is exactly what _emit_income posted.
            for fee_plan in fee_plans:
                emit_fee_cycle(
                    ctx,
                    writer,
                    fee_plan,
                    month_start=month_start,
                    monthly_credit_cents=(
                        salary[index] if fee_plan.account is deposit else Cents(0)
                    ),
                    relationship_balance_cents=relationship_balance,
                )
                emit_wire_activity(
                    ctx, writer, fee_plan, month_start=month_start, coverage=fee_coverage
                )

        # ---------------------------------------------------------------- life-event evidence
        for event in plan.life_events:
            _emit_corroboration(ctx, plan, writer, event, deposit=deposit, months=months)

        # ---------------------------------------------------------------- adversarial seed (2.7)
        # A merchant name is attacker-influencable text that reaches a prompt through expense
        # analytics. One transaction per carrier, so the payload is present without distorting the
        # spend profile it sits in.
        probe_merchant = adversarial_merchant(ctx, plan)
        if probe_merchant is not None:
            probe_account = cards[0] if cards else deposit
            if probe_account is not None:
                writer.write(
                    account=probe_account,
                    when=ctx.day_in_month(months[-1]),
                    amount=-ctx.cents((2_500, 18_000), round_to=50),
                    transaction_type=vocab.TXN_TYPE_DEBIT,
                    category="SHOPPING",
                    merchant=probe_merchant,
                    channel="CARD" if cards else "ONLINE",
                    status=vocab.TXN_STATUS_POSTED,
                )

        plan.monthly_expense = expense_total.divide(expense_months) if expense_months else Cents(0)
