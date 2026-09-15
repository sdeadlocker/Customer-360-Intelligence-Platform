"""Task 4.4: the field-masking serializer and its transforms."""

from __future__ import annotations

from datetime import date

from c360.domain.enums import (
    AccountStatus,
    AccountType,
    CustomerSegment,
    CustomerType,
    CustomerValue,
    DelinquencyStatus,
)
from c360.domain.models import (
    Account,
    ContactInfo,
    CreditCard,
    Customer,
    FinancialProfile,
    RiskProfile,
)
from c360.domain.money import Cents
from c360.security.masking import (
    band_currency,
    band_score,
    partial_last4,
    partial_name,
    partial_year,
)
from c360.security.model import Role
from c360.security.policy import policy_for_role
from c360.security.serializer import mask_model


def _customer() -> Customer:
    return Customer(
        customer_id="C-1",
        customer_name="Renata Alvarez",
        customer_type=CustomerType.INDIVIDUAL,
        customer_segment=CustomerSegment.AFFLUENT,
        customer_since=date(2014, 3, 17),
        date_of_birth=date(1982, 11, 4),
        customer_value=CustomerValue.GOLD,
        as_of_date=date(2026, 9, 1),
        source_system="CORE",
    )


def _risk() -> RiskProfile:
    return RiskProfile(
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
    )


class TestTransforms:
    def test_last4_keeps_only_the_tail(self) -> None:
        assert partial_last4("100200300") == "**0300"

    def test_last4_redacts_short_values(self) -> None:
        assert partial_last4("1234") == "***"

    def test_year_only(self) -> None:
        assert partial_year("1982-11-04") == "1982"

    def test_partial_name_keeps_first_token(self) -> None:
        assert partial_name("Renata Alvarez") == "Renata ***"
        assert partial_name("Cher") == "Cher"

    def test_currency_bands_are_coarse(self) -> None:
        assert band_currency(0) == "$0"
        assert band_currency(50_000) == "<$1K"
        assert band_currency(4_325_000) == "$10K-$100K"
        assert band_currency(-4_325_000) == "$10K-$100K"

    def test_score_bands(self) -> None:
        assert band_score(72.0) == "HIGH"
        assert band_score(85.0) == "VERY_HIGH"
        assert band_score(742.0, fico=True) == "VERY_GOOD"

    def test_none_passes_through_every_transform(self) -> None:
        assert partial_last4(None) is None
        assert partial_year(None) is None
        assert band_currency(None) is None
        assert band_score(None) is None


class TestHiddenIsAbsent:
    def test_a_hidden_field_has_no_key_in_the_payload(self) -> None:
        """Requirement 12.4: the unmasked value is not present in the payload at all."""
        data, masked = mask_model(_customer(), policy_for_role(Role.MARKETING))
        # Marketing HIDES date of birth entirely.
        assert "date_of_birth" not in data
        assert "date_of_birth" in masked

    def test_hidden_aml_flag_is_absent_for_contact_center(self) -> None:
        data, masked = mask_model(_risk(), policy_for_role(Role.CONTACT_CENTER))
        assert "aml_flag" not in data
        assert "pep_flag" not in data
        assert "aml_flag" in masked


class TestPartialAndBand:
    def test_contact_center_sees_year_only_dob(self) -> None:
        data, _ = mask_model(_customer(), policy_for_role(Role.CONTACT_CENTER))
        assert data["date_of_birth"] == "1982"

    def test_rm_sees_banded_risk_scores(self) -> None:
        data, masked = mask_model(_risk(), policy_for_role(Role.RM))
        assert data["risk_score"] == "HIGH"
        assert "risk_score" in masked

    def test_risk_analyst_sees_raw_scores(self) -> None:
        data, masked = mask_model(_risk(), policy_for_role(Role.RISK))
        assert data["risk_score"] == 72.0
        assert masked == []


class TestNestedMasking:
    def test_account_number_is_partial_and_reported_with_a_dotted_path(self) -> None:
        card = CreditCard(
            account_id="A-1",
            card_last4="4321",
            credit_limit_cents=Cents(500_000),
            overlimit_events=0,
            fraud_alerts=0,
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        )
        # Marketing HIDES card number entirely, even the last four.
        data, masked = mask_model(card, policy_for_role(Role.MARKETING))
        assert "card_last4" not in data
        assert "card_last4" in masked

    def test_full_pan_is_never_a_field_at_all(self) -> None:
        """The model has no card_number field, so no masking rule can forget it (design §4.3)."""
        assert "card_number" not in CreditCard.model_fields


class TestUnmaskedRolesSeeEverything:
    def test_rm_financials_are_full(self) -> None:
        profile = FinancialProfile(
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
        )
        data, masked = mask_model(profile, policy_for_role(Role.RM))
        assert masked == []
        assert data["net_worth_cents"] == 1_740_000

    def test_contact_center_gets_banded_balances(self) -> None:
        account = Account(
            account_id="A-1",
            customer_id="C-1",
            account_number="100200300",
            account_type=AccountType.DEPOSIT,
            balance_cents=Cents(450_000),
            account_status=AccountStatus.ACTIVE,
            open_date=date(2014, 3, 17),
            as_of_date=date(2026, 9, 1),
            source_system="CORE",
        )
        data, masked = mask_model(account, policy_for_role(Role.CONTACT_CENTER))
        assert data["balance_cents"] == "$1K-$10K"
        assert data["account_number"] == "**0300"
        assert "balance_cents" in masked
        assert "account_number" in masked


def test_marketing_email_is_hidden_but_city_survives_partial_address() -> None:
    contact = ContactInfo(
        customer_id="C-1",
        email="renata@example.invalid",
        phone_number="+1-614-555-0142",
        address_line1="4820 Kestrel Lane",
        city="Columbus",
        state="OH",
        country="US",
        postal_code="43215",
        as_of_date=date(2026, 9, 1),
        source_system="CORE",
    )
    # Contact Center: street address PARTIAL (city survives), contact FULL.
    data, masked = mask_model(contact, policy_for_role(Role.CONTACT_CENTER))
    assert data["city"] == "Columbus"
    assert "address_line1" in masked
    assert data["email"] == "renata@example.invalid"
