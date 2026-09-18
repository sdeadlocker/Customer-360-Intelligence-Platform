"""SQLite adapter for the fee-recovery reads (Phase 22 play 6).

Fee recovery asks, per deposit account per billing cycle, whether the bank was entitled to a fee and
whether it took it. Answering that for one customer needs four aggregates and one scalar, and every
one of them folds in SQL rather than in Python: a 24-month window over a customer's transactions is
a few thousand rows, and moving them into the process to count them would spend the endpoint's whole
latency budget on data movement for arithmetic SQLite does in the index.

Cycle keys are ``substr(transaction_date, 1, 7)``
------------------------------------------------

Dates are fixed-width ISO text (design §1.1), so a month bucket is a substring, and using ``substr``
rather than ``strftime`` keeps ``ix_txn_cust_date`` usable — a function over the column would not
be. The same trick is already load-bearing in :mod:`c360.data.repositories.financial`.

Fees are identified by category and merchant
--------------------------------------------

``txn`` has no fee-type column, so a maintenance fee is ``transaction_category = 'FEES'`` with the
maintenance merchant, and its reversal is the same pair with a positive amount. Those literals are
:mod:`c360.domain.fees` constants shared with the generator rather than strings repeated here, and
they are bound as parameters, never interpolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from c360.data.repositories.base import SqliteRepository, Statement
from c360.domain.enums import AccountType
from c360.domain.fees import (
    FEE_CATEGORY,
    FEE_MERCHANT_MAINTENANCE,
    FEE_MERCHANT_WIRE,
    WIRE_EVENT_MERCHANT,
)
from c360.domain.money import Cents

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "FeeAccountRow",
    "FeeCycleRow",
    "SqliteFeeRecoveryRepository",
    "WireActivityRow",
]


@dataclass(frozen=True, slots=True)
class FeeAccountRow:
    """One deposit account, with the columns the fee rules are evaluated against."""

    account_id: str
    account_number: str
    product_code: str | None
    product_name: str | None
    balance_cents: Cents
    account_status: str
    open_date: str
    as_of_date: str
    source_system: str


@dataclass(frozen=True, slots=True)
class FeeCycleRow:
    """One account's fee activity in one billing cycle.

    ``charged_cents`` and ``reversed_cents`` are magnitudes, both non-negative: the sign convention
    is resolved in SQL so no caller has to remember that a debit is negative.
    """

    account_id: str
    month: str
    charged_cents: Cents
    reversed_cents: Cents
    charge_count: int
    reversal_count: int


@dataclass(frozen=True, slots=True)
class WireActivityRow:
    """Outgoing wires an account sent, against the wire fees it was billed."""

    account_id: str
    wire_events: int
    wire_fees: int


_DEPOSIT_ACCOUNTS = Statement(
    id="fee_recovery.deposit_accounts",
    collection="account",
    sql="""
    SELECT a.account_id, a.account_number, a.product_code, a.product_name, a.balance_cents,
           a.account_status, a.open_date, a.as_of_date, c.source_system
    FROM account a
    JOIN customer c ON c.customer_id = a.customer_id
    JOIN deposit d ON d.account_id = a.account_id
    WHERE a.customer_id = :customer_id
      AND a.account_type = :deposit_type
    ORDER BY a.account_id
    """,
)

_MAINTENANCE_CYCLES = Statement(
    id="fee_recovery.maintenance_cycles",
    collection="txn",
    sql="""
    SELECT t.account_id,
           substr(t.transaction_date, 1, 7) AS month,
           SUM(CASE WHEN t.amount_cents < 0 THEN -t.amount_cents ELSE 0 END) AS charged_cents,
           SUM(CASE WHEN t.amount_cents > 0 THEN  t.amount_cents ELSE 0 END) AS reversed_cents,
           SUM(CASE WHEN t.amount_cents < 0 THEN 1 ELSE 0 END) AS charge_count,
           SUM(CASE WHEN t.amount_cents > 0 THEN 1 ELSE 0 END) AS reversal_count
    FROM txn t
    WHERE t.customer_id = :customer_id
      AND t.transaction_date >= :start_date
      AND t.transaction_category = :fee_category
      AND t.merchant = :maintenance_merchant
    GROUP BY t.account_id, month
    ORDER BY t.account_id, month
    """,
)

_QUALIFYING_CREDITS = Statement(
    id="fee_recovery.qualifying_credits",
    collection="txn",
    sql="""
    SELECT t.account_id,
           substr(t.transaction_date, 1, 7) AS month,
           SUM(t.amount_cents) AS credit_cents
    FROM txn t
    WHERE t.customer_id = :customer_id
      AND t.transaction_date >= :start_date
      AND t.amount_cents > 0
      AND t.transaction_category <> :fee_category
    GROUP BY t.account_id, month
    ORDER BY t.account_id, month
    """,
)

_WIRE_ACTIVITY = Statement(
    id="fee_recovery.wire_activity",
    collection="txn",
    sql="""
    SELECT t.account_id,
           SUM(CASE WHEN t.merchant = :wire_event_merchant THEN 1 ELSE 0 END) AS wire_events,
           SUM(CASE WHEN t.merchant = :wire_fee_merchant THEN 1 ELSE 0 END) AS wire_fees
    FROM txn t
    WHERE t.customer_id = :customer_id
      AND t.transaction_date >= :start_date
      AND t.merchant IN (:wire_event_merchant, :wire_fee_merchant)
    GROUP BY t.account_id
    ORDER BY t.account_id
    """,
)

_RELATIONSHIP_BALANCE = Statement(
    id="fee_recovery.relationship_balance",
    collection="financial_profile",
    sql="""
    SELECT COALESCE(fp.total_deposits_cents, 0) + COALESCE(fp.total_investments_cents, 0)
    FROM financial_profile fp
    WHERE fp.customer_id = :customer_id
    """,
)


class SqliteFeeRecoveryRepository(SqliteRepository):
    """Reads backing the fee-recovery scan. Read-only over ``customer.db``."""

    __slots__ = ()

    def deposit_accounts(self, customer_id: str) -> tuple[FeeAccountRow, ...]:
        """Every deposit account the customer holds, open or not.

        Closed and dormant accounts are returned rather than filtered in SQL because the *service*
        decides what is billable — a closed account is not, but that judgement belongs with the
        rules that also know a certificate has no maintenance fee, not spread across two layers.
        """
        rows = self.fetch_all(
            _DEPOSIT_ACCOUNTS,
            {"customer_id": customer_id, "deposit_type": AccountType.DEPOSIT.value},
        )
        return tuple(
            FeeAccountRow(
                account_id=row.account_id,
                account_number=row.account_number,
                product_code=row.product_code,
                product_name=row.product_name,
                balance_cents=Cents(row.balance_cents),
                account_status=row.account_status,
                open_date=row.open_date,
                as_of_date=row.as_of_date,
                source_system=row.source_system,
            )
            for row in rows
        )

    def maintenance_cycles(self, customer_id: str, *, start_date: str) -> tuple[FeeCycleRow, ...]:
        """Maintenance-fee charges and reversals per account per cycle since ``start_date``."""
        rows = self.fetch_all(
            _MAINTENANCE_CYCLES,
            {
                "customer_id": customer_id,
                "start_date": start_date,
                "fee_category": FEE_CATEGORY,
                "maintenance_merchant": FEE_MERCHANT_MAINTENANCE,
            },
        )
        return tuple(
            FeeCycleRow(
                account_id=row.account_id,
                month=row.month,
                charged_cents=Cents(row.charged_cents or 0),
                reversed_cents=Cents(row.reversed_cents or 0),
                charge_count=int(row.charge_count or 0),
                reversal_count=int(row.reversal_count or 0),
            )
            for row in rows
        )

    def qualifying_credits(
        self, customer_id: str, *, start_date: str
    ) -> dict[tuple[str, str], Cents]:
        """Credits per account per cycle, keyed ``(account_id, month)``.

        Fee reversals are excluded: a reversed fee is not income arriving in the account, and
        letting one count toward a direct-deposit waiver would let a waiver justify itself.
        """
        rows = self.fetch_all(
            _QUALIFYING_CREDITS,
            {
                "customer_id": customer_id,
                "start_date": start_date,
                "fee_category": FEE_CATEGORY,
            },
        )
        return {(row.account_id, row.month): Cents(row.credit_cents or 0) for row in rows}

    def wire_activity(self, customer_id: str, *, start_date: str) -> tuple[WireActivityRow, ...]:
        """Outgoing wire counts against wire-fee counts, per account."""
        rows = self.fetch_all(
            _WIRE_ACTIVITY,
            {
                "customer_id": customer_id,
                "start_date": start_date,
                "wire_event_merchant": WIRE_EVENT_MERCHANT,
                "wire_fee_merchant": FEE_MERCHANT_WIRE,
            },
        )
        return tuple(
            WireActivityRow(
                account_id=row.account_id,
                wire_events=int(row.wire_events or 0),
                wire_fees=int(row.wire_fees or 0),
            )
            for row in rows
        )

    def relationship_balance(self, customer_id: str) -> Cents:
        """Deposits plus investments, the basis a relationship waiver is tested against.

        Zero when the customer has no financial profile, which fails toward "not waived" — the
        conservative direction is to consider the cycle billable and let the fee evidence decide.
        """
        value = self.fetch_scalar(_RELATIONSHIP_BALANCE, {"customer_id": customer_id}, default=0)
        return Cents(int(value))


def statement_ids() -> Sequence[str]:
    """The statement IDs this adapter issues, for the query-plan and no-N+1 assertions."""
    return (
        _DEPOSIT_ACCOUNTS.id,
        _MAINTENANCE_CYCLES.id,
        _QUALIFYING_CREDITS.id,
        _WIRE_ACTIVITY.id,
        _RELATIONSHIP_BALANCE.id,
    )
