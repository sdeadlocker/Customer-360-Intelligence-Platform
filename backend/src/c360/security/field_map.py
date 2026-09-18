"""Which model field belongs to which masking group, and how its partial form renders (task 4.4).

The design 7.2 matrix is expressed in *field groups* ("Account number", "Balances, net worth"),
not in the concrete column names the models carry. This module is the bridge: for each domain model
that reaches a response, it declares a :class:`FieldRule` per sensitive field — the
:class:`FieldGroup` that decides *whether* the role may see it, and the :class:`MaskStyle` that
decides *how* its PARTIAL/BAND form is rendered.

A field with no rule is unmasked for every role. That is the correct default only because the
sensitive fields are exhaustively enumerated here and the role x field test (task 4.5) asserts every
matrix group is represented — an omission is a test failure, not a silent leak. The rules live in
one module so that assertion has one place to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from c360.security.masking import MaskStyle
from c360.security.model import FieldGroup


@dataclass(frozen=True, slots=True)
class FieldRule:
    """The masking rule for one model field."""

    group: FieldGroup
    style: MaskStyle = MaskStyle.TEXT


# Per-model field rules, keyed by the model class name so the serializer can look up rules without
# importing the model classes (which would couple the security layer to the domain layer's shape).
# Only sensitive fields appear; everything else is FULL for all roles.
_RULES: Final[dict[str, dict[str, FieldRule]]] = {
    "Customer": {
        "customer_name": FieldRule(FieldGroup.IDENTITY, MaskStyle.NAME),
        "customer_segment": FieldRule(FieldGroup.IDENTITY),
        "customer_value": FieldRule(FieldGroup.IDENTITY),
        "date_of_birth": FieldRule(FieldGroup.DATE_OF_BIRTH, MaskStyle.YEAR_ONLY),
    },
    "ContactInfo": {
        "email": FieldRule(FieldGroup.CONTACT),
        "phone_number": FieldRule(FieldGroup.CONTACT),
        "mobile_number": FieldRule(FieldGroup.CONTACT),
        "address_line1": FieldRule(FieldGroup.STREET_ADDRESS),
        "address_line2": FieldRule(FieldGroup.STREET_ADDRESS),
        "postal_code": FieldRule(FieldGroup.STREET_ADDRESS),
        # city / state / country stay visible: PARTIAL on street address means "city only", so the
        # city is the *surviving* field, not a masked one.
    },
    "FinancialProfile": {
        "total_deposits_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "total_loans_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "total_investments_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "total_assets_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "total_liabilities_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "net_worth_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "household_net_worth_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "monthly_income_cents": FieldRule(FieldGroup.INCOME_EXPENSE, MaskStyle.CURRENCY),
        "monthly_expense_cents": FieldRule(FieldGroup.INCOME_EXPENSE, MaskStyle.CURRENCY),
    },
    "CreditProfile": {
        "fico_score": FieldRule(FieldGroup.CREDIT_SCORE, MaskStyle.SCORE),
        "behavior_score": FieldRule(FieldGroup.CREDIT_SCORE, MaskStyle.SCORE),
        "propensity_score": FieldRule(FieldGroup.OFFERS, MaskStyle.SCORE),
        "credit_exposure_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
    },
    "Account": {
        "account_number": FieldRule(FieldGroup.ACCOUNT_NUMBER, MaskStyle.LAST4),
        "balance_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "available_balance_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
    },
    "Loan": {
        "loan_number": FieldRule(FieldGroup.ACCOUNT_NUMBER, MaskStyle.LAST4),
        "original_amount_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "monthly_emi_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
    },
    "CreditCard": {
        # card_last4 is itself the partial form of a PAN — but the card-number group still governs
        # it, so Marketing (HIDDEN) never even sees the last four.
        "card_last4": FieldRule(FieldGroup.CARD_NUMBER, MaskStyle.TEXT),
        "credit_limit_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "rewards_balance_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "monthly_spend_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
    },
    "Investment": {
        "portfolio_value_cents": FieldRule(FieldGroup.INVESTMENT_DETAIL, MaskStyle.CURRENCY),
        "asset_allocation": FieldRule(FieldGroup.INVESTMENT_DETAIL),
        "mutual_funds_cents": FieldRule(FieldGroup.INVESTMENT_DETAIL, MaskStyle.CURRENCY),
        "stocks_cents": FieldRule(FieldGroup.INVESTMENT_DETAIL, MaskStyle.CURRENCY),
        "bonds_cents": FieldRule(FieldGroup.INVESTMENT_DETAIL, MaskStyle.CURRENCY),
        "retirement_accounts_cents": FieldRule(FieldGroup.INVESTMENT_DETAIL, MaskStyle.CURRENCY),
    },
    "Transaction": {
        "merchant": FieldRule(FieldGroup.MERCHANT_DETAIL),
    },
    "RiskProfile": {
        "risk_score": FieldRule(FieldGroup.RISK_SCORES, MaskStyle.SCORE),
        "fraud_score": FieldRule(FieldGroup.RISK_SCORES, MaskStyle.SCORE),
        "pid_score": FieldRule(FieldGroup.RISK_SCORES, MaskStyle.SCORE),
        "sid_score": FieldRule(FieldGroup.RISK_SCORES, MaskStyle.SCORE),
        "delinquency_status": FieldRule(FieldGroup.DELINQUENCY),
        "current_days_past_due": FieldRule(FieldGroup.DELINQUENCY, MaskStyle.SCORE),
        "default_indicator": FieldRule(FieldGroup.DELINQUENCY),
        "chargeoff_indicator": FieldRule(FieldGroup.DELINQUENCY),
        "aml_flag": FieldRule(FieldGroup.AML_PEP),
        "pep_flag": FieldRule(FieldGroup.AML_PEP),
    },
    "PropertyDetail": {
        "address_line1": FieldRule(FieldGroup.VIN_PROPERTY),
        "postal_code": FieldRule(FieldGroup.VIN_PROPERTY),
    },
    "VehicleDetail": {
        "vin": FieldRule(FieldGroup.VIN_PROPERTY, MaskStyle.VIN),
    },
    "CustomerOffer": {
        "acceptance_probability": FieldRule(FieldGroup.OFFERS, MaskStyle.SCORE),
        "probability_confidence": FieldRule(FieldGroup.OFFERS),
        "expected_value_cents": FieldRule(FieldGroup.OFFERS, MaskStyle.CURRENCY),
    },
    # Phase 17 signals feed. A signal's value-at-stake is a monetary amount (an exposure or a
    # deposit), so it is governed by the BALANCES group exactly as any balance is: a role without
    # balance entitlement sees a band, never the figure. No other signal field is maskable —
    # evidence summaries and detail chips are value-free labels by construction (task 17.2).
    "SignalModel": {
        "value_at_stake_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
    },
    # Fee recovery (Phase 22 play 6). The recoverable amounts are balances in every sense that
    # matters to the policy matrix, so a role that sees banded balances sees banded recoveries. Note
    # what is *not* here: `account_label` needs no rule because it is partial by construction
    # (product name plus the last four), so the full account number never reaches the serializer at
    # all — the same reasoning that keeps a full PAN off `CreditCard`.
    "FeeRecoveryResponse": {
        "monthly_recoverable_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "annualized_recoverable_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
    },
    "FeeFindingModel": {
        "monthly_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
        "annualized_cents": FieldRule(FieldGroup.BALANCES, MaskStyle.CURRENCY),
    },
}


def rules_for(model_name: str) -> dict[str, FieldRule]:
    """Return the masking rules for a model, or an empty mapping if it has no sensitive fields."""
    return _RULES.get(model_name, {})


def all_declared_groups() -> set[FieldGroup]:
    """Every field group that appears in the map. The matrix test asserts this covers the matrix."""
    return {rule.group for rules in _RULES.values() for rule in rules.values()}
