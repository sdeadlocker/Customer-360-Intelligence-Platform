"""``FinancialService`` — holdings, profiles and expense analytics (task 5.2, design §6.1).

Holdings, the financial profile and the credit profile are straight pass-throughs to the repository;
the service earns its place with the expense analytics of requirement 5.8/5.9, which the repository
deliberately leaves to it. The repository returns category totals and a monthly series in exact
integer cents; the service turns the series into deviation flags against a trailing six-month
baseline.

The deviation rule (requirement 5.9)
------------------------------------

For each month, the baseline is the mean of the *preceding* six months' total spend (all
categories),
and a month is flagged when it lies more than ``spend_anomaly_sigma`` standard deviations from that
baseline. A month with fewer than a minimum of prior months has no stable baseline and is not
flagged — a customer's first months on file should not read as anomalies. The comparison is the one
place a float legitimately appears in this layer: a standard deviation is not a monetary value, and
requirement 5.9 asks for a statistical deviation, not an exact-cents equality. The cents totals
themselves are never rounded; only the derived sigma multiple is floating point, and it decides a
boolean flag, not a displayed amount.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

from c360.domain.models import MonthlyTotal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from c360.data.repositories.financial import SqliteFinancialRepository
    from c360.domain.enums import AccountStatus, AccountType
    from c360.domain.models import (
        CategoryTotal,
        CreditProfile,
        FinancialProfile,
        Holding,
        Transaction,
    )

#: A month needs at least this many preceding months before it has a baseline worth deviating from.
#: Below it, the standard deviation is either undefined or dominated by noise, so no flag is raised.
_MIN_BASELINE_MONTHS = 3

#: The trailing window the baseline is computed over (requirement 5.9's "trailing 6-month average").
_BASELINE_WINDOW = 6


@dataclass(frozen=True, slots=True)
class MonthlyFlag:
    """A month of total spend and whether it deviated from its trailing baseline (requirement 5.9).

    ``deviation_sigma`` is ``None`` when the month had no baseline (too few preceding months); it is
    the signed number of standard deviations from the baseline otherwise, so a caller can render
    both "unusually high" and "unusually low" and sort by magnitude.
    """

    month: str
    total_cents: int
    transaction_count: int
    is_anomaly: bool
    deviation_sigma: float | None


@dataclass(frozen=True, slots=True)
class ExpenseAnalytics:
    """The expense-analytics read model behind requirement 5.8/5.9.

    ``by_category`` is the category distribution (spend only, debits re-signed positive),
    ``monthly``
    is the trend series oldest-first with each month's deviation flag, and ``threshold_sigma`` is
    the
    configured sensitivity, echoed so the UI can label what "anomaly" meant for this response.
    """

    by_category: tuple[CategoryTotal, ...]
    monthly: tuple[MonthlyFlag, ...]
    threshold_sigma: float


class FinancialService:
    """Holdings, profiles and expense analytics over the financial repository."""

    __slots__ = ("_anomaly_sigma", "_repository")

    def __init__(
        self,
        repository: SqliteFinancialRepository,
        *,
        spend_anomaly_sigma: float,
    ) -> None:
        if spend_anomaly_sigma <= 0:
            raise ValueError(f"spend_anomaly_sigma must be positive, got {spend_anomaly_sigma}")
        self._repository = repository
        self._anomaly_sigma = spend_anomaly_sigma

    # ---------------------------------------------------------------- profiles (req 5.1, 5.10)
    def get_financial_profile(self, customer_id: str) -> FinancialProfile | None:
        return self._repository.get_financial_profile(customer_id)

    def get_credit_profile(self, customer_id: str) -> CreditProfile | None:
        return self._repository.get_credit_profile(customer_id)

    # ---------------------------------------------------------------- holdings (req 5.3-5.7)
    def get_holdings(
        self,
        customer_id: str,
        *,
        account_types: Sequence[AccountType] | None = None,
        statuses: Sequence[AccountStatus] | None = None,
    ) -> Sequence[Holding]:
        return self._repository.list_holdings(
            customer_id, account_types=account_types, statuses=statuses
        )

    def get_transactions(
        self,
        customer_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        categories: Sequence[str] | None = None,
        limit: int = 500,
    ) -> Sequence[Transaction]:
        return self._repository.list_transactions(
            customer_id,
            start_date=start_date,
            end_date=end_date,
            categories=categories,
            limit=limit,
        )

    # ---------------------------------------------------------------- analytics (req 5.8, 5.9)
    def get_expense_analytics(
        self,
        customer_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        months: int = 12,
    ) -> ExpenseAnalytics:
        """Category distribution plus a monthly trend with trailing-baseline deviation flags."""
        by_category = tuple(
            self._repository.totals_by_category(
                customer_id, start_date=start_date, end_date=end_date
            )
        )
        monthly = self._repository.monthly_totals(customer_id, months=months)
        return ExpenseAnalytics(
            by_category=by_category,
            monthly=self._flag_months(monthly),
            threshold_sigma=self._anomaly_sigma,
        )

    def _flag_months(self, monthly: Sequence[MonthlyTotal]) -> tuple[MonthlyFlag, ...]:
        """Flag each month against the mean and stdev of its preceding window (requirement 5.9)."""
        flags: list[MonthlyFlag] = []
        for index, point in enumerate(monthly):
            window = [int(p.total_cents) for p in monthly[max(0, index - _BASELINE_WINDOW) : index]]
            sigma, is_anomaly = self._deviation(int(point.total_cents), window)
            flags.append(
                MonthlyFlag(
                    month=point.month,
                    total_cents=int(point.total_cents),
                    transaction_count=point.transaction_count,
                    is_anomaly=is_anomaly,
                    deviation_sigma=sigma,
                )
            )
        return tuple(flags)

    def _deviation(self, value: int, window: list[int]) -> tuple[float | None, bool]:
        """Signed sigma of ``value`` from ``window``'s baseline, and whether it exceeds threshold.

        Returns ``(None, False)`` when the window is too short or has no spread — a zero-variance
        window (every prior month identical) has no scale on which to call anything anomalous, so a
        single different month is reported with its magnitude only if the spread is real.
        """
        if len(window) < _MIN_BASELINE_MONTHS:
            return None, False
        mean = statistics.fmean(window)
        stdev = statistics.pstdev(window)
        if stdev == 0:
            return None, False
        sigma = (value - mean) / stdev
        return sigma, abs(sigma) >= self._anomaly_sigma
