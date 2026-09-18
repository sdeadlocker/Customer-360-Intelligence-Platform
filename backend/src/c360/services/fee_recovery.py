"""Fee recovery (Phase 22 play 6) — charges the bank earned but never collected.

Over years, deposit billing drifts. A courtesy waiver is granted once and never restored. A product
reprices and an account stays on the retired amount. A billable service goes live without billing.
No single line is large enough for anyone to notice, which is exactly why it accumulates.

This service finds those gaps deterministically and prices them. It is the platform's cheapest play
to justify: no customer acquisition, no credit risk, no persuasion — the money was already earned,
it simply was not billed, and it is recovered in the next cycle.

Four findings, each grounded in a document
------------------------------------------

``MISSED_MINIMUM``    the cycle did not qualify for a waiver and no fee was posted.
``LEGACY_PRICING``    a fee was posted, but below the current schedule.
``FEE_WAIVED``        a fee was posted and reversed, for a run longer than the courtesy allowance.
``UNBILLED_SERVICE``  a billable event occurred with no accompanying fee.

Every finding names the product sheet its amount comes from, so a finding is auditable rather than
asserted. The amounts come from :class:`c360.domain.fees.FeeSchedule`, transcribed from the corpus;
this module contributes only the arithmetic, in integer cents throughout.

What the data can and cannot support — read this before trusting a number
------------------------------------------------------------------------

The schema stores a *current* balance per account and no historical balance series, so no cycle's
actual balance is knowable. The balance condition is therefore evaluated against the account's as-of
balance for every cycle, and the per-cycle variable is the month's qualifying credit. A real
fee-recovery engine would use daily balance history; this one says so, in the evidence line of every
finding whose basis is a balance.

The consequence is directional and deliberate. Where the evidence is ambiguous the scan declines to
raise a finding: a missing measurement is treated as "possibly waived" rather than "billable", so
the error is a missed recovery rather than a false accusation. Under-claiming is recoverable;
presenting a bank with fees it was not entitled to is not.

Nothing here writes. The scan is a read over the read-only customer database, computed on demand, so
there is no detection job to schedule and no state to invalidate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from c360.domain.money import Cents

if TYPE_CHECKING:
    from collections.abc import Iterable

    from c360.data.repositories.fee_recovery import (
        FeeAccountRow,
        FeeCycleRow,
        SqliteFeeRecoveryRepository,
    )
    from c360.domain.fees import FeeSchedule, ProductFeeRule

__all__ = ["FeeFinding", "FeeRecoveryService", "FeeRecoveryView", "LeakType"]

#: Cycles per year, for annualizing a monthly gap.
_MONTHS_IN_YEAR: Final = 12

#: Account statuses that can still be billed. A closed account cannot, and a dormant one is governed
#: by ``pol-dormant-account`` rather than the product's maintenance schedule, so neither is scanned.
_BILLABLE_STATUSES: Final = frozenset({"ACTIVE"})

#: The last four of an account number is the most identifying fragment allowed into a finding label.
#: The full number never enters this module's output at all, so no masking rule has to remember to
#: redact it (requirement 12.5).
_LAST4 = 4


class LeakType(StrEnum):
    """The kinds of recoverable billing gap this scan reports."""

    FEE_WAIVED = "FEE_WAIVED"
    LEGACY_PRICING = "LEGACY_PRICING"
    UNBILLED_SERVICE = "UNBILLED_SERVICE"
    MISSED_MINIMUM = "MISSED_MINIMUM"


@dataclass(frozen=True, slots=True)
class FeeFinding:
    """One recoverable billing gap on one account."""

    finding_id: int
    leak_type: LeakType
    #: Pre-masked: product name plus the last four of the account number, never the full number.
    account_label: str
    monthly_cents: Cents
    annualized_cents: Cents
    #: The corpus document the amount is measured against, and its id, so the UI can cite it.
    rule_basis: str
    doc_id: str
    #: How the gap was established, in one sentence, including the basis used for a balance test.
    evidence: str
    #: Billing cycles the gap spans. One for a per-event finding.
    cycles: int
    status: str = "OPEN"


@dataclass(frozen=True, slots=True)
class FeeRecoveryView:
    """The customer's recoverable fee income and the findings behind it."""

    monthly_recoverable_cents: Cents
    annualized_recoverable_cents: Cents
    findings: tuple[FeeFinding, ...]
    as_of_date: str
    source_system: str


class FeeRecoveryService:
    """Scans a customer's deposit billing against the fee schedule."""

    __slots__ = ("_courtesy_cycles", "_lookback_months", "_min_finding", "_repository", "_schedule")

    def __init__(
        self,
        repository: SqliteFeeRecoveryRepository,
        schedule: FeeSchedule,
        *,
        courtesy_waiver_cycles: int,
        min_finding_cents: int,
        lookback_months: int,
    ) -> None:
        if courtesy_waiver_cycles < 0:
            raise ValueError(
                f"courtesy_waiver_cycles cannot be negative, got {courtesy_waiver_cycles}"
            )
        if min_finding_cents < 0:
            raise ValueError(f"min_finding_cents cannot be negative, got {min_finding_cents}")
        if lookback_months < 1:
            raise ValueError(f"lookback_months must be positive, got {lookback_months}")
        self._repository = repository
        self._schedule = schedule
        self._courtesy_cycles = courtesy_waiver_cycles
        self._min_finding = Cents(min_finding_cents)
        self._lookback_months = lookback_months

    # ---------------------------------------------------------------- entry point
    def get_fee_recovery(self, customer_id: str, *, as_of: str | None = None) -> FeeRecoveryView:
        """Scan the customer's deposit accounts and price every billing gap found.

        Returns an empty view — not ``None`` — for a customer with nothing recoverable. "No leakage"
        is a real, useful answer here, and a 404 would make a clean book indistinguishable from a
        missing one.
        """
        accounts = self._repository.deposit_accounts(customer_id)
        as_of_date = as_of or _latest_as_of(accounts)
        start_date = _window_start(as_of_date, self._lookback_months)

        cycles_by_account = _group_cycles(
            self._repository.maintenance_cycles(customer_id, start_date=start_date)
        )
        credits_by_cycle = self._repository.qualifying_credits(customer_id, start_date=start_date)
        wires = {
            row.account_id: row
            for row in self._repository.wire_activity(customer_id, start_date=start_date)
        }
        relationship_balance = self._repository.relationship_balance(customer_id)
        expected_months = _expected_months(as_of_date, self._lookback_months)

        findings: list[FeeFinding] = []
        for account in accounts:
            rule = self._schedule.rule_for(account.product_code)
            if rule is None or account.account_status not in _BILLABLE_STATUSES:
                # Nothing billable on this product, or the account cannot be billed at all.
                continue
            account_cycles = cycles_by_account.get(account.account_id, {})
            findings.extend(
                self._scan_account(
                    account,
                    rule,
                    cycles=account_cycles,
                    credits_by_cycle=credits_by_cycle,
                    relationship_balance=relationship_balance,
                    expected_months=expected_months,
                )
            )
            wire_row = wires.get(account.account_id)
            if wire_row is not None:
                unbilled = self._unbilled_wires(
                    account, rule, wire_row.wire_events, wire_row.wire_fees
                )
                if unbilled is not None:
                    findings.append(unbilled)

        ordered = _rank(findings)
        return FeeRecoveryView(
            monthly_recoverable_cents=_total(f.monthly_cents for f in ordered),
            annualized_recoverable_cents=_total(f.annualized_cents for f in ordered),
            findings=ordered,
            as_of_date=as_of_date,
            source_system=_source_system(accounts),
        )

    # ---------------------------------------------------------------- per-account detectors
    def _scan_account(
        self,
        account: FeeAccountRow,
        rule: ProductFeeRule,
        *,
        cycles: dict[str, FeeCycleRow],
        credits_by_cycle: dict[tuple[str, str], Cents],
        relationship_balance: Cents,
        expected_months: tuple[str, ...],
    ) -> list[FeeFinding]:
        billable_unbilled = 0
        reversal_cycles = 0
        shortfall_cycles = 0
        shortfall_total = Cents(0)
        opened_month = account.open_date[:7]

        for month in expected_months:
            if month < opened_month:
                continue
            cycle = cycles.get(month)
            waived = rule.waiver.waives(
                balance_cents=account.balance_cents,
                monthly_credit_cents=credits_by_cycle.get((account.account_id, month), Cents(0)),
                relationship_balance_cents=relationship_balance,
            )
            if cycle is None or cycle.charge_count == 0:
                # Nothing posted. Only a cycle that did not qualify for a waiver is recoverable.
                if not waived:
                    billable_unbilled += 1
                continue
            if cycle.reversal_count > 0 and cycle.reversed_cents >= cycle.charged_cents:
                # Posted and given straight back.
                reversal_cycles += 1
                continue
            if cycle.charged_cents < rule.maintenance_fee_cents:
                shortfall_cycles += 1
                shortfall_total += rule.maintenance_fee_cents - cycle.charged_cents

        out: list[FeeFinding] = []
        missed = self._missed_minimum(account, rule, billable_unbilled)
        if missed is not None:
            out.append(missed)
        legacy = self._legacy_pricing(account, rule, shortfall_cycles, shortfall_total)
        if legacy is not None:
            out.append(legacy)
        waived_finding = self._waiver_pattern(account, rule, reversal_cycles)
        if waived_finding is not None:
            out.append(waived_finding)
        return out

    def _missed_minimum(
        self, account: FeeAccountRow, rule: ProductFeeRule, unbilled_cycles: int
    ) -> FeeFinding | None:
        """A run of cycles that owed the maintenance fee and were never charged it."""
        if unbilled_cycles == 0:
            return None
        recoverable = rule.maintenance_fee_cents * unbilled_cycles
        if recoverable < self._min_finding:
            return None
        return _finding(
            leak_type=LeakType.MISSED_MINIMUM,
            account=account,
            rule=rule,
            monthly=rule.maintenance_fee_cents,
            cycles=unbilled_cycles,
            evidence=(
                f"{unbilled_cycles} of the last {self._lookback_months} cycles did not qualify "
                f"for a waiver ({rule.waiver.explain()}) yet no maintenance fee was posted. "
                f"Balance tested against the account's as-of balance, the only balance the "
                f"source data carries."
            ),
        )

    def _legacy_pricing(
        self,
        account: FeeAccountRow,
        rule: ProductFeeRule,
        shortfall_cycles: int,
        shortfall_total: Cents,
    ) -> FeeFinding | None:
        """Fees posted below the current schedule — a grandfathered amount never migrated."""
        if shortfall_cycles == 0 or shortfall_total < self._min_finding:
            return None
        return _finding(
            leak_type=LeakType.LEGACY_PRICING,
            account=account,
            rule=rule,
            monthly=shortfall_total.divide(shortfall_cycles),
            cycles=shortfall_cycles,
            evidence=(
                f"{shortfall_cycles} cycles were billed below the current schedule of "
                f"{_dollars(rule.maintenance_fee_cents)}, leaving "
                f"{_dollars(shortfall_total)} uncollected across the window."
            ),
        )

    def _waiver_pattern(
        self, account: FeeAccountRow, rule: ProductFeeRule, reversal_cycles: int
    ) -> FeeFinding | None:
        """Fees posted then reversed for longer than courtesy service recovery explains."""
        chargeable = reversal_cycles - self._courtesy_cycles
        if chargeable <= 0:
            return None
        recoverable = rule.maintenance_fee_cents * chargeable
        if recoverable < self._min_finding:
            return None
        return _finding(
            leak_type=LeakType.FEE_WAIVED,
            account=account,
            rule=rule,
            monthly=rule.maintenance_fee_cents,
            cycles=chargeable,
            doc_id=self._schedule.waiver_policy_doc_id or rule.doc_id,
            rule_basis=self._schedule.waiver_policy_rule_basis,
            evidence=(
                f"The maintenance fee was posted and reversed in {reversal_cycles} consecutive "
                f"cycles. The first {self._courtesy_cycles} are treated as service recovery — a "
                f"configured allowance, not a published rule — leaving {chargeable} cycles "
                f"to review."
            ),
        )

    def _unbilled_wires(
        self, account: FeeAccountRow, rule: ProductFeeRule, events: int, billed: int
    ) -> FeeFinding | None:
        """Outgoing wires sent without the per-wire fee the product sheet prices."""
        wire_fee = rule.outgoing_wire_fee_cents
        if wire_fee is None or events <= billed:
            return None
        unbilled = events - billed
        recoverable = wire_fee * unbilled
        if recoverable < self._min_finding:
            return None
        return _finding(
            leak_type=LeakType.UNBILLED_SERVICE,
            account=account,
            rule=rule,
            monthly=recoverable.divide(self._lookback_months),
            cycles=unbilled,
            evidence=(
                f"{unbilled} of {events} outgoing wires were sent without the "
                f"{_dollars(wire_fee)} per-wire fee the product sheet prices."
            ),
        )


# ---------------------------------------------------------------- helpers
def _finding(
    *,
    leak_type: LeakType,
    account: FeeAccountRow,
    rule: ProductFeeRule,
    monthly: Cents,
    cycles: int,
    evidence: str,
    doc_id: str | None = None,
    rule_basis: str | None = None,
) -> FeeFinding:
    """Assemble a finding.

    ``annualized_cents`` is the recoverable *run rate*, not the sum found in the window: a $30 gap
    repeating monthly is $360 a year, and reporting only what the window happened to contain would
    understate a live problem and overstate a closed one. The window total rides in the evidence,
    where it can be read without being mistaken for a forward-looking figure.
    """
    return FeeFinding(
        finding_id=_finding_id(account.account_id, leak_type),
        leak_type=leak_type,
        account_label=_label(account),
        monthly_cents=monthly,
        annualized_cents=monthly * _MONTHS_IN_YEAR,
        rule_basis=rule_basis or rule.rule_basis,
        doc_id=doc_id or rule.doc_id,
        evidence=evidence,
        cycles=cycles,
        status="OPEN",
    )


def _finding_id(account_id: str, leak_type: LeakType) -> int:
    """A stable, non-negative id for one (account, leak type) pair.

    Stable so the UI's per-row actions survive a refetch, and derived rather than sequential so it
    does not depend on scan order. A 32-bit fold of a SHA-256 digest: this is a display key, never a
    security control.
    """
    digest = hashlib.sha256(f"{account_id}:{leak_type.value}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def _label(account: FeeAccountRow) -> str:
    """``Premier Checking ****4821`` — product name plus the last four, never the full number."""
    name = account.product_name or account.product_code or "Deposit account"
    tail = account.account_number[-_LAST4:] if account.account_number else ""
    return f"{name} ****{tail}" if tail else name


def _group_cycles(rows: tuple[FeeCycleRow, ...]) -> dict[str, dict[str, FeeCycleRow]]:
    grouped: dict[str, dict[str, FeeCycleRow]] = {}
    for row in rows:
        grouped.setdefault(row.account_id, {})[row.month] = row
    return grouped


def _rank(findings: list[FeeFinding]) -> tuple[FeeFinding, ...]:
    """Largest annualized recovery first, then by label and type so the order is total.

    A stable total order matters because the UI renders the list and the export writes it: two calls
    must not disagree about which finding is first.
    """
    return tuple(
        sorted(
            findings,
            key=lambda f: (-int(f.annualized_cents), f.account_label, f.leak_type.value),
        )
    )


def _total(amounts: Iterable[Cents]) -> Cents:
    """Sum cents exactly. Integer arithmetic throughout, so no rounding residue accumulates."""
    return Cents(sum(int(amount) for amount in amounts))


def _expected_months(as_of_date: str, lookback_months: int) -> tuple[str, ...]:
    """The ``YYYY-MM`` keys of the last ``lookback_months`` **closed** cycles, oldest first.

    The as-of month is deliberately excluded. A maintenance fee is assessed at cycle end, so the
    current month has not been billed yet and a fee cannot be "missing" from it — including it would
    manufacture one phantom unbilled cycle for every billable account on the book. The window
    therefore ends at the month before the as-of date, which is also the newest month the source
    data can be complete for.
    """
    year, month = int(as_of_date[:4]), int(as_of_date[5:7])
    # Index of the as-of month on an absolute month scale, then step back one to the last closed
    # one.
    last_closed = (year * _MONTHS_IN_YEAR + month - 1) - 1
    keys: list[str] = []
    for offset in range(lookback_months - 1, -1, -1):
        total = last_closed - offset
        keys.append(f"{total // _MONTHS_IN_YEAR:04d}-{total % _MONTHS_IN_YEAR + 1:02d}")
    return tuple(keys)


def _window_start(as_of_date: str, lookback_months: int) -> str:
    """First day of the oldest cycle in the window, as an ISO date to bind."""
    first = _expected_months(as_of_date, lookback_months)[0]
    return f"{first}-01"


def _latest_as_of(accounts: tuple[FeeAccountRow, ...]) -> str:
    """The newest ``as_of_date`` among the accounts, or today when there are none."""
    if not accounts:
        from c360.domain.dates import today_iso  # noqa: PLC0415 - only this fallback needs it

        return today_iso()
    return max(account.as_of_date for account in accounts)


def _source_system(accounts: tuple[FeeAccountRow, ...]) -> str:
    return accounts[0].source_system if accounts else "CORE_BANKING"


#: Cents per dollar for the evidence text. Presentation only; the ledger stays in cents.
_CENTS_PER_DOLLAR: Final = 100


def _dollars(amount: Cents) -> str:
    return f"${int(amount) // _CENTS_PER_DOLLAR:,}"
