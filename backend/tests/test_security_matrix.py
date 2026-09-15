"""Task 4.5: the role x field security matrix test.

This is the test design §20 names as the mitigation for "masking bypass via a new endpoint": it
asserts, for every role against every sensitive field group, that the resolved mode is exactly the
design §7.2 value, and - critically - that an unmasked value is **absent from the serialized
payload**, not merely hidden in the UI (requirement 12.4). It also covers the role x
knowledge-access-level matrix.

The expected matrix below is transcribed independently from the design document, deliberately *not*
imported from :mod:`c360.security.policy`. If it were the same table, the test would only prove the
table equals itself. Two independent transcriptions that must agree is what catches a
transcription error in either.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from c360.domain.enums import (
    AccountStatus,
    AccountType,
    CustomerSegment,
    CustomerType,
    CustomerValue,
    DelinquencyStatus,
    InvestmentRiskProfile,
)
from c360.domain.models import (
    Account,
    ContactInfo,
    CreditCard,
    CreditProfile,
    Customer,
    CustomerOffer,
    FinancialProfile,
    Investment,
    Loan,
    RiskProfile,
    Transaction,
    VehicleDetail,
)
from c360.domain.money import Bps, Cents
from c360.security.field_map import all_declared_groups
from c360.security.model import (
    FieldGroup,
    KnowledgeLevel,
    MaskMode,
    Role,
    knowledge_levels_for_role,
)
from c360.security.policy import policy_for_role
from c360.security.serializer import mask_model

# ---------------------------------------------------------------- expected matrix (independent)
_F, _P, _H, _B = MaskMode.FULL, MaskMode.PARTIAL, MaskMode.HIDDEN, MaskMode.BAND

# Row order and values transcribed by hand from design §7.2. Column order:
# RM, WEALTH_ADVISOR, CONTACT_CENTER, BRANCH, RISK, MARKETING.
_EXPECTED: dict[FieldGroup, tuple[MaskMode, ...]] = {
    FieldGroup.IDENTITY: (_F, _F, _F, _F, _F, _P),
    FieldGroup.DATE_OF_BIRTH: (_F, _F, _P, _P, _F, _H),
    FieldGroup.STREET_ADDRESS: (_F, _F, _P, _P, _F, _H),
    FieldGroup.CONTACT: (_F, _F, _F, _F, _F, _H),
    FieldGroup.ACCOUNT_NUMBER: (_P, _P, _P, _P, _P, _H),
    FieldGroup.CARD_NUMBER: (_P, _P, _P, _P, _P, _H),
    FieldGroup.BALANCES: (_F, _F, _B, _B, _F, _B),
    FieldGroup.INCOME_EXPENSE: (_F, _F, _H, _B, _F, _B),
    FieldGroup.INVESTMENT_DETAIL: (_F, _F, _H, _H, _F, _H),
    FieldGroup.CREDIT_SCORE: (_F, _F, _B, _B, _F, _B),
    FieldGroup.RISK_SCORES: (_B, _B, _B, _B, _F, _H),
    FieldGroup.DELINQUENCY: (_F, _F, _B, _B, _F, _H),
    FieldGroup.AML_PEP: (_F, _F, _H, _H, _F, _H),
    FieldGroup.VIN_PROPERTY: (_P, _P, _H, _H, _F, _H),
    FieldGroup.MERCHANT_DETAIL: (_F, _F, _P, _P, _F, _H),
    # RISK is the demo "full access" role: it sees every business field group in full (offers and
    # investment detail included). Account/card numbers stay PARTIAL for every role by compliance.
    FieldGroup.OFFERS: (_F, _F, _F, _F, _F, _F),
}

_ROLE_ORDER: tuple[Role, ...] = (
    Role.RM,
    Role.WEALTH_ADVISOR,
    Role.CONTACT_CENTER,
    Role.BRANCH,
    Role.RISK,
    Role.MARKETING,
)

_EXPECTED_KNOWLEDGE: dict[Role, set[KnowledgeLevel]] = {
    Role.RM: {KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL},
    Role.WEALTH_ADVISOR: {KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL},
    Role.CONTACT_CENTER: {KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL},
    Role.BRANCH: {KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL},
    Role.RISK: {
        KnowledgeLevel.PUBLIC,
        KnowledgeLevel.INTERNAL,
        KnowledgeLevel.RISK_ONLY,
        KnowledgeLevel.COMPLIANCE_ONLY,
    },
    Role.MARKETING: {KnowledgeLevel.PUBLIC},
}


@pytest.mark.parametrize("group", list(FieldGroup))
@pytest.mark.parametrize("role_index", range(6))
def test_every_role_field_cell_matches_the_design_matrix(
    group: FieldGroup, role_index: int
) -> None:
    role = _ROLE_ORDER[role_index]
    expected = _EXPECTED[group][role_index]
    actual = policy_for_role(role).mode_for(group)
    assert actual is expected, f"{role} x {group}: expected {expected}, got {actual}"


def test_the_field_map_covers_every_matrix_group() -> None:
    """Every group in the matrix must be reachable from a real field, or masking it is untested."""
    declared = all_declared_groups()
    missing = set(FieldGroup) - declared
    assert not missing, f"field groups with no field mapping: {sorted(str(g) for g in missing)}"


def test_the_knowledge_matrix_matches_the_design() -> None:
    for role, expected in _EXPECTED_KNOWLEDGE.items():
        actual = set(policy_for_role(role).modes)  # touch the policy to ensure it resolves
        assert actual  # sanity
        assert set(knowledge_levels_for_role(role)) == expected, role


# ---------------------------------------------------------------- absence-in-payload proof
def _full_models() -> list[object]:
    """One instance of each maskable model, fully populated with sensitive values."""
    return [
        Customer(
            customer_id="C-1",
            customer_name="Renata Alvarez",
            customer_type=CustomerType.INDIVIDUAL,
            customer_segment=CustomerSegment.AFFLUENT,
            customer_since=date(2014, 3, 17),
            date_of_birth=date(1982, 11, 4),
            customer_value=CustomerValue.GOLD,
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        ContactInfo(
            customer_id="C-1",
            email="renata@example.invalid",
            phone_number="+1-614-555-0142",
            mobile_number="+1-614-555-0199",
            address_line1="4820 Kestrel Lane",
            city="Columbus",
            state="OH",
            country="US",
            postal_code="43215",
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        FinancialProfile(
            customer_id="C-1",
            total_deposits_cents=Cents(1_850_000),
            total_loans_cents=Cents(2_500_000),
            total_investments_cents=Cents(1_200_000),
            total_assets_cents=Cents(4_325_000),
            total_liabilities_cents=Cents(2_585_000),
            net_worth_cents=Cents(1_740_000),
            monthly_income_cents=Cents(920_000),
            monthly_expense_cents=Cents(610_000),
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        CreditProfile(
            customer_id="C-1",
            fico_score=742,
            behavior_score=610,
            propensity_score=63.25,
            credit_utilization_bps=Bps(1700),
            credit_exposure_cents=Cents(3_500_000),
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        Account(
            account_id="A-1",
            customer_id="C-1",
            account_number="100200300",
            account_type=AccountType.DEPOSIT,
            balance_cents=Cents(450_000),
            available_balance_cents=Cents(445_000),
            account_status=AccountStatus.ACTIVE,
            open_date=date(2014, 3, 17),
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        Loan(
            account_id="A-2",
            loan_number="700800900",
            original_amount_cents=Cents(3_200_000),
            monthly_emi_cents=Cents(19_700),
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        CreditCard(
            account_id="A-3",
            card_last4="9977",
            credit_limit_cents=Cents(500_000),
            monthly_spend_cents=Cents(85_000),
            overlimit_events=0,
            fraud_alerts=1,
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        Investment(
            account_id="A-4",
            portfolio_value_cents=Cents(1_200_000),
            mutual_funds_cents=Cents(600_000),
            stocks_cents=Cents(400_000),
            bonds_cents=Cents(200_000),
            investment_risk_profile=InvestmentRiskProfile.MODERATE,
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        Transaction(
            transaction_id="T-1",
            account_id="A-1",
            customer_id="C-1",
            transaction_date=date(2026, 8, 1),
            amount_cents=Cents(-5000),
            transaction_type="DEBIT",
            transaction_category="GROCERIES",
            merchant="Kroger Marketplace",
            status="POSTED",
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        RiskProfile(
            customer_id="C-1",
            risk_score=72.0,
            fraud_score=10.0,
            pid_score=5.0,
            sid_score=3.0,
            delinquency_status=DelinquencyStatus.DPD_1_29,
            current_days_past_due=12,
            default_indicator=False,
            chargeoff_indicator=False,
            aml_flag=True,
            pep_flag=True,
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        VehicleDetail(
            asset_id="AS-1",
            make="Subaru",
            model="Outback",
            model_year=2021,
            vin="4S4BSANC1M3200001",
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        CustomerOffer(
            customer_offer_id="CO-1",
            customer_id="C-1",
            offer_id="O-1",
            event_date=date(2026, 7, 1),
            customer_reaction="PENDING",
            acceptance_probability=0.42,
            expected_value_cents=Cents(120_000),
            is_suppressed=False,
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
    ]


#: Per-model raw secret values with the group that governs each. Values are distinctive enough that
#: a substring match cannot collide with an unrelated field (a card last-4 is a full, unique token
#: here, not four digits that could appear inside a postal code). A secret must be absent from the
#: payload whenever its governing group is not FULL for the role.
_MODEL_SECRETS: dict[str, list[tuple[FieldGroup, str]]] = {
    "ContactInfo": [
        (FieldGroup.CONTACT, "renata@example.invalid"),
        (FieldGroup.CONTACT, "+1-614-555-0142"),
        (FieldGroup.CONTACT, "+1-614-555-0199"),
        (FieldGroup.STREET_ADDRESS, "4820 Kestrel Lane"),
    ],
    "Account": [(FieldGroup.ACCOUNT_NUMBER, "100200300")],
    "Loan": [(FieldGroup.ACCOUNT_NUMBER, "700800900")],
    "CreditCard": [(FieldGroup.CARD_NUMBER, "9977")],
    "VehicleDetail": [(FieldGroup.VIN_PROPERTY, "4S4BSANC1M3200001")],
    "Transaction": [(FieldGroup.MERCHANT_DETAIL, "Kroger Marketplace")],
}


@pytest.mark.parametrize("role", list(Role))
def test_masked_values_are_absent_from_the_serialized_payload(role: Role) -> None:
    """For every model and role, no raw secret survives unless its group is FULL for that role."""
    policy = policy_for_role(role)
    for model in _full_models():
        secrets = _MODEL_SECRETS.get(type(model).__name__)
        if not secrets:
            continue
        data, _ = mask_model(model, policy)  # type: ignore[arg-type]
        blob = json.dumps(data)
        for group, secret in secrets:
            if policy.mode_for(group) is MaskMode.FULL:
                continue
            assert secret not in blob, (
                f"{role}: raw {group} value {secret!r} leaked into a "
                f"{type(model).__name__} payload"
            )


def test_marketing_never_sees_any_risk_indicator() -> None:
    """Marketing HIDES risk scores, delinquency and AML/PEP — none may appear in any form."""
    data, masked = mask_model(
        RiskProfile(
            customer_id="C-1",
            risk_score=72.0,
            fraud_score=10.0,
            pid_score=5.0,
            sid_score=3.0,
            delinquency_status=DelinquencyStatus.DPD_90_PLUS,
            current_days_past_due=120,
            default_indicator=True,
            chargeoff_indicator=True,
            aml_flag=True,
            pep_flag=True,
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        ),
        policy_for_role(Role.MARKETING),
    )
    for field in (
        "risk_score",
        "fraud_score",
        "pid_score",
        "sid_score",
        "delinquency_status",
        "aml_flag",
        "pep_flag",
        "default_indicator",
        "chargeoff_indicator",
    ):
        assert field not in data, field
        assert field in masked, field
