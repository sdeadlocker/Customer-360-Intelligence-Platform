"""Fee postings and deliberate fee leakage (Phase 22 play 6, extending task 2.4).

The seeded dataset had no fee transactions at all, which meant the fee-recovery play had nothing to
detect. This module supplies them, and — more importantly — supplies *wrong* ones on purpose.

Why leakage is seeded rather than hoped for
-------------------------------------------

Task 2.6 seeds life events "with corroborating transactions so agent inferences are verifiable", and
task 2.7 seeds adversarial text so the Phase 11 red-team suite has something real to resist. Fee
recovery gets the same treatment: each of the four leak types the detector claims to find is planted
here, deterministically, so a test can assert the detector finds it rather than asserting only that
it does not crash. A detector verified against data containing no defects is a detector that has
never been tested.

The four planted behaviours, drawn once per account
--------------------------------------------------

A behaviour is drawn per *account*, not per cycle, because that is how fee leakage actually happens:
an account is mispriced, or a waiver is left in place, and it stays that way for months. Drawing per
cycle would produce noise that no rule could characterise.

``CORRECT``   bills every cycle the schedule says is billable. The majority case — a platform where
              most billing is right is the only one where a finding means anything.
``MISSED``    never posts the fee even though the cycle was billable. The MISSED_MINIMUM finding.
``WAIVED``    posts the fee and immediately reverses it, every cycle. The FEE_WAIVED finding, once
              the run of reversals exceeds the configured courtesy allowance.
``LEGACY``    posts a grandfathered amount below the current schedule. The LEGACY_PRICING finding.

Business accounts additionally send outgoing wires, and a subset of those go unbilled, which is the
UNBILLED_SERVICE finding.

What a cycle's waiver test can and cannot see
---------------------------------------------

The schema stores a *current* balance per account and no historical balance series, so neither this
module nor the detector can know what the balance was in a cycle eighteen months ago. Both therefore
evaluate the balance condition against the account's as-of balance, and the per-cycle variable is
the month's qualifying credit. That is a real limitation of the data model rather than a shortcut,
and the detector's evidence says which basis it used. Both sides call the same
:meth:`c360.domain.fees.WaiverRule.waives`, so the rule cannot drift between them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from c360.domain.enums import AccountStatus, AccountType
from c360.domain.fees import (
    FEE_CATEGORY,
    FEE_MERCHANT_MAINTENANCE,
    FEE_MERCHANT_WIRE,
    WIRE_EVENT_MERCHANT,
    ProductFeeRule,
)
from c360.domain.money import Bps, Cents
from c360.generator import vocab

if TYPE_CHECKING:
    from datetime import date

    from c360.generator.context import GeneratorContext
    from c360.generator.plan import AccountRecord, CustomerPlan

__all__ = [
    "FeeBehaviour",
    "FeeCoverage",
    "FeePlan",
    "emit_fee_cycle",
    "emit_wire_activity",
    "plan_account_fees",
]

#: Channel for a system-posted fee. Fees are not customer-initiated, so none of the interactive
#: channels in :data:`vocab.TXN_CHANNELS` describe them.
_FEE_CHANNEL: Final = "SYSTEM"

#: Channel for an outgoing wire instruction.
_WIRE_CHANNEL: Final = "WIRE"


class FeeBehaviour(StrEnum):
    """How one account's fees are billed across the whole window."""

    CORRECT = "CORRECT"
    MISSED = "MISSED"
    WAIVED = "WAIVED"
    LEGACY = "LEGACY"


#: Behaviour mix. Weighted so correct billing dominates and each defect is present but uncommon —
#: roughly three in ten accounts carrying a defect is enough for every detector to have several
#: findings across the seeded book without the data reading as a bank that bills nothing correctly.
_BEHAVIOUR_WEIGHTS: Final[tuple[tuple[FeeBehaviour, int], ...]] = (
    (FeeBehaviour.CORRECT, 70),
    (FeeBehaviour.MISSED, 12),
    (FeeBehaviour.WAIVED, 10),
    (FeeBehaviour.LEGACY, 8),
)

#: A grandfathered fee as a share of the current schedule, in percent. Bounded well below 100 so the
#: shortfall clears any sane minimum-finding floor, and above zero so it reads as old pricing rather
#: than as a fee that was never charged (which is a different finding).
_LEGACY_SHARE_PERCENT: Final = (35, 65)

#: Outgoing wires a business account sends in a month, and the chance it sends any at all.
_WIRES_PER_MONTH: Final = (1, 3)
_WIRE_MONTH_PERCENT: Final = 55

#: Chance that a wire this month goes unbilled. Per wire, not per account: unbilled service usually
#: looks like a few postings that slipped, not a switch left off.
_UNBILLED_WIRE_PERCENT: Final = 30

#: Amount range for the wire principal itself, in cents. The fee is what matters to this play; the
#: principal only has to look like a business payment.
_WIRE_AMOUNT_CENTS: Final = (150_000, 4_500_000)


@dataclass(slots=True)
class FeeCoverage:
    """A population-level floor guaranteeing each planted defect occurs at least once.

    Task 2.1 gives each persona cohort "a per-cohort minimum floor so no cohort rounds to zero", for
    the same reason this exists: at a small ``--count`` a weighted draw can legitimately produce
    zero of a rare behaviour, and a seeded dataset that happens to contain no mispriced account
    makes the corresponding detector untestable. Rather than raise the defect rate — which would
    misrepresent a bank as billing mostly wrongly — the first billable accounts encountered are
    *assigned* one defect each, and everything after them draws from the weighted mix.

    Mutable and threaded through the customer loop, so the guarantee is over the whole population
    rather than per customer. Order depends only on iteration order, so output stays reproducible.
    """

    #: Defects not yet placed. Popped in order; empty means the floor is satisfied.
    pending: list[FeeBehaviour]
    #: Whether one wire has been deliberately left unbilled yet.
    unbilled_wire_placed: bool = False

    @classmethod
    def create(cls) -> FeeCoverage:
        return cls(pending=[FeeBehaviour.MISSED, FeeBehaviour.WAIVED, FeeBehaviour.LEGACY])


@dataclass(frozen=True, slots=True)
class FeePlan:
    """The billing behaviour settled on for one account, fixed for the whole window."""

    account: AccountRecord
    rule: ProductFeeRule
    behaviour: FeeBehaviour
    #: What actually gets posted when a fee is posted. Equal to the schedule amount except under
    #: ``LEGACY``, where it is the grandfathered amount and the difference is the recoverable gap.
    posted_fee_cents: Cents

    @property
    def bills_a_fee(self) -> bool:
        """Whether a billable cycle results in a posting at all."""
        return self.behaviour is not FeeBehaviour.MISSED

    @property
    def reverses_the_fee(self) -> bool:
        """Whether a posted fee is immediately credited back."""
        return self.behaviour is FeeBehaviour.WAIVED


def plan_account_fees(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    schedule_lookup: dict[str, ProductFeeRule],
    *,
    relationship_balance_cents: Cents,
    coverage: FeeCoverage,
) -> list[FeePlan]:
    """Decide each fee-bearing deposit account's billing behaviour for the run.

    Only open deposit accounts whose product code appears in the fee schedule are considered: a
    product the schedule omits (a certificate) has nothing billable, so it gets no plan and
    therefore no postings and no findings.

    A defect is only planted on an account that is **plausibly billable** — one whose balance and
    relationship balance alone do not already waive the fee. Two reasons. It is what actually
    happens: an account that is never charged cannot have its charge mispriced or left waived. And
    it is what makes the plant *observable* — a defect on a permanently-waived account produces no
    posting, no absence of a posting, and so nothing any detector could find, which would quietly
    thin the signal the Phase 22 tests depend on.
    """
    plans: list[FeePlan] = []
    for account in plan.accounts_of(AccountType.DEPOSIT):
        if account.status is not AccountStatus.ACTIVE:
            continue
        rule = schedule_lookup.get(account.product_code)
        if rule is None:
            continue
        # Credit is excluded on purpose: it varies per cycle, so "billable ignoring credit" is the
        # stable plan-time question. An account waived only by a monthly credit still bills in any
        # cycle the credit fails to arrive, which is a real defect opportunity.
        already_waived = rule.waiver.waives(
            balance_cents=account.balance,
            monthly_credit_cents=Cents(0),
            relationship_balance_cents=relationship_balance_cents,
        )
        if already_waived:
            behaviour = FeeBehaviour.CORRECT
        elif coverage.pending:
            # Satisfy the population floor before spending draws on the weighted mix.
            behaviour = coverage.pending.pop(0)
        else:
            behaviour = ctx.weighted(_BEHAVIOUR_WEIGHTS)
        plans.append(
            FeePlan(
                account=account,
                rule=rule,
                behaviour=behaviour,
                posted_fee_cents=_posted_amount(ctx, rule, behaviour),
            )
        )
    return plans


def _posted_amount(ctx: GeneratorContext, rule: ProductFeeRule, behaviour: FeeBehaviour) -> Cents:
    """The amount a posting carries: the schedule, or a grandfathered share of it under LEGACY."""
    if behaviour is not FeeBehaviour.LEGACY:
        return rule.maintenance_fee_cents
    share = Bps(ctx.integer(_LEGACY_SHARE_PERCENT) * 100)
    # Never let rounding collapse a legacy fee to zero: that would read as "never charged", which is
    # a different finding, and would make the LEGACY plant undetectable as legacy.
    return Cents(max(1, int(rule.maintenance_fee_cents.apply_bps(share))))


def emit_fee_cycle(
    ctx: GeneratorContext,
    writer: _FeeWriter,
    fee_plan: FeePlan,
    *,
    month_start: date,
    monthly_credit_cents: Cents,
    relationship_balance_cents: Cents,
) -> None:
    """Post (or deliberately fail to post) one account's maintenance fee for one cycle.

    A cycle before the account was opened is skipped: billing an account that did not exist would be
    a data defect rather than a planted one, and the detector correctly ignores such cycles too.
    """
    account = fee_plan.account
    if account.open_date.replace(day=1) > month_start:
        return

    waived = fee_plan.rule.waiver.waives(
        balance_cents=account.balance,
        monthly_credit_cents=monthly_credit_cents,
        relationship_balance_cents=relationship_balance_cents,
    )
    if waived:
        # Legitimately waived: the schedule says nothing is owed, so nothing is posted and nothing
        # is recoverable. The detector must agree, which is why both sides call the same rule.
        return

    if not fee_plan.bills_a_fee:
        return

    amount = fee_plan.posted_fee_cents
    charged_on = ctx.day_in_month(month_start, low=22, high=28)
    writer.write(
        account=account,
        when=charged_on,
        amount=-amount,
        transaction_type=vocab.TXN_TYPE_DEBIT,
        category=FEE_CATEGORY,
        merchant=FEE_MERCHANT_MAINTENANCE,
        channel=_FEE_CHANNEL,
        status=vocab.TXN_STATUS_POSTED,
    )
    if fee_plan.reverses_the_fee:
        # The reversal is a credit of the same amount, same merchant, same cycle. That triple is
        # what makes a waiver identifiable at all — the schema has no reversal flag.
        writer.write(
            account=account,
            when=charged_on,
            amount=amount,
            transaction_type=vocab.TXN_TYPE_CREDIT,
            category=FEE_CATEGORY,
            merchant=FEE_MERCHANT_MAINTENANCE,
            channel=_FEE_CHANNEL,
            status=vocab.TXN_STATUS_POSTED,
        )


def emit_wire_activity(
    ctx: GeneratorContext,
    writer: _FeeWriter,
    fee_plan: FeePlan,
    *,
    month_start: date,
    coverage: FeeCoverage,
) -> None:
    """Send this cycle's outgoing wires, billing only some of them.

    Only for a product whose sheet prices a wire. Premier checking includes domestic wires at no
    charge, so its ``outgoing_wire_fee_cents`` is absent and an outgoing wire there is correctly
    unbilled — the detector must not report it, which is exactly why the fee lives on the rule
    rather than being assumed for every product.
    """
    wire_fee = fee_plan.rule.outgoing_wire_fee_cents
    if wire_fee is None:
        return
    account = fee_plan.account
    if account.open_date.replace(day=1) > month_start:
        return
    if not ctx.chance(_WIRE_MONTH_PERCENT):
        return

    for _ in range(ctx.integer(_WIRES_PER_MONTH)):
        sent_on = ctx.day_in_month(month_start, low=2, high=26)
        writer.write(
            account=account,
            when=sent_on,
            amount=-ctx.cents(_WIRE_AMOUNT_CENTS, round_to=100),
            transaction_type=vocab.TXN_TYPE_DEBIT,
            category="TRANSFER",
            merchant=WIRE_EVENT_MERCHANT,
            channel=_WIRE_CHANNEL,
            status=vocab.TXN_STATUS_POSTED,
        )
        # The first wire the population sends is always left unbilled, so the unbilled-service
        # detector has a guaranteed case at any `--count` (see FeeCoverage).
        if not coverage.unbilled_wire_placed:
            coverage.unbilled_wire_placed = True
            continue
        if ctx.chance(_UNBILLED_WIRE_PERCENT):
            continue
        writer.write(
            account=account,
            when=sent_on,
            amount=-wire_fee,
            transaction_type=vocab.TXN_TYPE_DEBIT,
            category=FEE_CATEGORY,
            merchant=FEE_MERCHANT_WIRE,
            channel=_FEE_CHANNEL,
            status=vocab.TXN_STATUS_POSTED,
        )


if TYPE_CHECKING:
    from typing import Protocol

    class _FeeWriter(Protocol):
        """The subset of the transaction writer this module needs.

        Declared structurally so this module does not import the writer's private class from
        :mod:`c360.generator.transactions`, which would make the dependency circular.
        """

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
        ) -> str: ...

else:  # pragma: no cover - the Protocol exists only for type checking
    _FeeWriter = object
