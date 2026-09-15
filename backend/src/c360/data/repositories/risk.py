"""SQLite adapter for :class:`~c360.domain.ports.RiskRepository` (task 1.6).

Requirement 8.7 hides detailed scores from a caller without the risk entitlement and shows only a
coarse band. That filtering is *not* here. It belongs to the field-masking serializer of task 4.4,
which applies on the response model so no handler and no tool can skip it. A repository that
pre-filtered by role would be a second, independent implementation of the same policy — and design
§7.2's whole argument for a single serializer-level filter is that two implementations eventually
disagree.
"""

from __future__ import annotations

from c360.data.repositories.base import SqliteRepository, Statement, optional_model
from c360.domain.models import RiskProfile

_RISK_PROFILE = Statement(
    id="risk.profile",
    collection="risk_profile",
    sql="""
    SELECT rp.customer_id, rp.risk_score, rp.fraud_score, rp.pid_score, rp.sid_score,
           rp.delinquency_status, rp.current_days_past_due, rp.default_indicator,
           rp.chargeoff_indicator, rp.aml_flag, rp.pep_flag, rp.as_of_date, c.source_system
    FROM risk_profile rp
    JOIN customer c ON c.customer_id = rp.customer_id
    WHERE rp.customer_id = :customer_id
    """,
)

_CREDIT_EXPOSURE = Statement(
    id="risk.credit_exposure",
    collection="account",
    # Design §4.6: credit exposure is loan balances plus card *limits*, not card balances. An
    # undrawn
    # limit is still exposure — the customer can draw it tomorrow — which is why requirement 8.5
    # asks
    # for exposure rather than for the sum of what is currently owed.
    #
    # Closed accounts are excluded: a closed facility cannot be drawn on. `COALESCE` turns SQLite's
    # NULL-for-an-empty-SUM into the zero that requirement 4.7 distinguishes from "not available".
    sql="""
    SELECT
      COALESCE((
        SELECT SUM(a.balance_cents)
        FROM account a JOIN loan l ON l.account_id = a.account_id
        WHERE a.customer_id = :customer_id AND a.account_status <> 'CLOSED'
      ), 0)
      +
      COALESCE((
        SELECT SUM(cc.credit_limit_cents)
        FROM account a JOIN credit_card cc ON cc.account_id = a.account_id
        WHERE a.customer_id = :customer_id AND a.account_status <> 'CLOSED'
      ), 0)
    """,
)


class SqliteRiskRepository(SqliteRepository):
    """Reads backing requirements 8.1 through 8.6."""

    def get_risk_profile(self, customer_id: str) -> RiskProfile | None:
        return optional_model(
            RiskProfile, self.fetch_one(_RISK_PROFILE, {"customer_id": customer_id})
        )

    def total_credit_exposure_cents(self, customer_id: str) -> int:
        return int(self.fetch_scalar(_CREDIT_EXPOSURE, {"customer_id": customer_id}, default=0))
