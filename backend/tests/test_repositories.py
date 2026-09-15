"""Repository adapter tests (task 1.6).

Covers the two halves of the Phase 1 gate this task owns: "repositories round-trip hand-seeded rows"
and "DB spans visible in traces".

Reads run against a **read-only** engine, the way the application opens the database (design §12.3).
That is not incidental: a repository that accidentally issued a write would pass against a
read-write
engine and fail in production.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Row, text

from c360.core.telemetry import SpanAttr
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.migrations import upgrade_database
from c360.data.repositories import (
    QueryMetrics,
    Repositories,
    SqliteOfferRepository,
    build_repositories,
)
from c360.data.repositories.base import (
    RepositoryError,
    Statement,
    expand_in_clause,
)
from c360.domain.enums import (
    AccountStatus,
    AccountType,
    ApplicationStatus,
    AssetType,
    BusinessGroup,
    CustomerReaction,
    CustomerSegment,
    CustomerType,
    CustomerValue,
    DelinquencyStatus,
    DepositProductType,
    EngagementChannel,
    EngagementEventType,
    HouseholdRole,
    InvestmentRiskProfile,
    LifeEventSource,
    LifeEventType,
    LoanType,
    OfferType,
    PartyRole,
    PropertyType,
    RelationshipType,
)
from c360.domain.models import (
    CreditCard,
    Deposit,
    Investment,
    Loan,
    PropertyDetail,
    VehicleDetail,
)
from c360.domain.money import Bps, Cents
from tests import seed_data
from tests.seed_data import PRIMARY_ID, SECONDARY_ID

# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated, seeded database. Module-scoped: the data is immutable, like the real one."""
    path = tmp_path_factory.mktemp("repositories") / "customer.db"
    upgrade_database(path)
    writer = create_sqlite_engine(path, mode=AccessMode.READ_WRITE, pool_size=1)
    try:
        with writer.begin() as connection:
            seed_data.seed(connection)
        # The seed has to satisfy referential integrity, or the tests are asserting against data the
        # application could never hold.
        with writer.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        writer.dispose()
    return path


@pytest.fixture
def metrics() -> QueryMetrics:
    return QueryMetrics()


@pytest.fixture
def repos(seeded_db: Path, metrics: QueryMetrics) -> Iterator[Repositories]:
    engine = create_sqlite_engine(seeded_db, mode=AccessMode.READ_ONLY, pool_size=4)
    try:
        yield build_repositories(engine, metrics=metrics)
    finally:
        engine.dispose()


# ================================================================ customer
def test_customer_round_trips(repos: Repositories) -> None:
    customer = repos.customer.get(PRIMARY_ID)
    assert customer is not None
    assert customer.customer_id == PRIMARY_ID
    assert customer.customer_name == "Renata Alvarez"
    assert customer.customer_type is CustomerType.INDIVIDUAL
    assert customer.customer_segment is CustomerSegment.AFFLUENT
    assert customer.customer_value is CustomerValue.GOLD
    # Dates arrive as ISO text and come out as real dates.
    assert customer.customer_since == date(2014, 3, 17)
    assert customer.date_of_birth == date(1982, 11, 4)
    assert customer.customer_value_score == pytest.approx(78.5)
    assert customer.preferred_language == "en"
    assert customer.household_id == seed_data.HOUSEHOLD_ID


def test_provenance_is_present_on_every_read(repos: Repositories) -> None:
    """Task 1.6: ``as_of_date`` and ``source_system`` carried on every read.

    Including on rows whose table has no ``source_system`` column of its own — ``contact_info``, the
    profile tables, ``account`` — where it is inherited from the aggregate root.
    """
    expected_as_of = date(2026, 9, 1)
    reads = [
        repos.customer.get(PRIMARY_ID),
        repos.customer.get_contact_info(PRIMARY_ID),
        repos.customer.get_employer_for_customer(PRIMARY_ID),
        repos.customer.get_household(seed_data.HOUSEHOLD_ID),
        repos.financial.get_financial_profile(PRIMARY_ID),
        repos.financial.get_credit_profile(PRIMARY_ID),
        repos.risk.get_risk_profile(PRIMARY_ID),
    ]
    for model in reads:
        assert model is not None
        assert model.as_of_date == expected_as_of
        assert model.source_system

    # Nested and specialization models too.
    for holding in repos.financial.list_holdings(PRIMARY_ID):
        assert holding.as_of_date == expected_as_of
        assert holding.source_system == seed_data.SOURCE_CORE
        assert holding.account.source_system == seed_data.SOURCE_CORE
        if holding.detail is not None:
            # Inherited from the parent account: the specialization tables carry neither column.
            assert holding.detail.as_of_date == expected_as_of
            assert holding.detail.source_system == seed_data.SOURCE_CORE

    for asset in repos.relationship.list_assets(PRIMARY_ID):
        assert asset.source_system == "COLLATERAL_SYS"
        assert asset.detail is not None
        assert asset.detail.source_system == "COLLATERAL_SYS"


def test_source_system_reflects_the_owning_system(repos: Repositories) -> None:
    """Distinct systems must survive the read rather than collapsing to one constant."""
    primary = repos.customer.get(PRIMARY_ID)
    secondary = repos.customer.get(SECONDARY_ID)
    assert primary is not None
    assert secondary is not None
    assert primary.source_system == seed_data.SOURCE_CORE
    assert secondary.source_system == seed_data.SOURCE_CRM

    contact = repos.customer.get_contact_info(PRIMARY_ID)
    assert contact is not None
    assert contact.source_system == seed_data.SOURCE_CORE


def test_missing_rows_return_none_not_an_exception(repos: Repositories) -> None:
    assert repos.customer.get("C-does-not-exist") is None
    assert repos.customer.get_contact_info(SECONDARY_ID) is None
    assert repos.customer.get_employer_for_customer(SECONDARY_ID) is None
    assert repos.customer.get_household("H-nope") is None
    assert repos.financial.get_credit_profile(SECONDARY_ID) is None
    assert repos.risk.get_risk_profile(SECONDARY_ID) is None
    assert repos.offer.get_campaign("CM-nope") is None


def test_exists_does_not_load_the_row(repos: Repositories, metrics: QueryMetrics) -> None:
    assert repos.customer.exists(PRIMARY_ID)
    assert not repos.customer.exists("C-nope")
    assert metrics.statements == ["customer.exists", "customer.exists"]


def test_sparse_fields_come_back_as_none_not_blank(repos: Repositories) -> None:
    """Requirement 4.7 needs "not available" to be distinguishable from an empty string or zero."""
    customer = repos.customer.get(SECONDARY_ID)
    assert customer is not None
    assert customer.date_of_birth is None
    assert customer.occupation is None
    assert customer.employer_id is None
    assert customer.preferred_channel is None
    assert customer.customer_value_score is None


def test_employer_is_read_through_the_customer(repos: Repositories) -> None:
    employer = repos.customer.get_employer_for_customer(PRIMARY_ID)
    assert employer is not None
    assert employer.employer_name == "Northwind Logistics"
    assert employer.industry == "Transportation"
    # Provenance is the customer's, because the fact is "this customer works here".
    assert employer.as_of_date == date(2026, 9, 1)
    assert employer.source_system == seed_data.SOURCE_CORE


def test_household_derives_a_source_system(repos: Repositories) -> None:
    household = repos.customer.get_household(seed_data.HOUSEHOLD_ID)
    assert household is not None
    assert household.member_count == 2
    assert household.primary_customer_id == PRIMARY_ID
    # Taken from the primary customer, not a constant.
    assert household.source_system == seed_data.SOURCE_CORE


def test_list_ids_paginates_on_the_primary_key(repos: Repositories) -> None:
    assert repos.customer.count() == 2
    assert list(repos.customer.list_ids(limit=10)) == [PRIMARY_ID, SECONDARY_ID]
    assert list(repos.customer.list_ids(limit=1)) == [PRIMARY_ID]
    assert list(repos.customer.list_ids(limit=10, after=PRIMARY_ID)) == [SECONDARY_ID]
    assert list(repos.customer.list_ids(limit=10, after=SECONDARY_ID)) == []


def test_list_ids_rejects_a_non_positive_limit(repos: Repositories) -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        repos.customer.list_ids(limit=0)


# ================================================================ financial
def test_financial_profile_uses_cents(repos: Repositories) -> None:
    profile = repos.financial.get_financial_profile(PRIMARY_ID)
    assert profile is not None
    assert isinstance(profile.net_worth_cents, Cents)
    assert profile.net_worth_cents == Cents(1_740_000)
    # Requirement 5.2, also enforced as a CHECK constraint.
    assert profile.net_worth_cents == profile.total_assets_cents - profile.total_liabilities_cents
    assert profile.net_worth_cents.to_decimal().as_tuple().exponent == -2


def test_credit_profile_uses_bps(repos: Repositories) -> None:
    profile = repos.financial.get_credit_profile(PRIMARY_ID)
    assert profile is not None
    assert profile.fico_score == 742
    assert isinstance(profile.credit_utilization_bps, Bps)
    assert profile.credit_utilization_bps == Bps(1_700)
    assert profile.credit_utilization_bps.to_percent() == pytest.approx(17)


def test_holdings_include_every_product_type_with_its_specialization(
    repos: Repositories,
) -> None:
    holdings = repos.financial.list_holdings(PRIMARY_ID)
    assert len(holdings) == 6  # 3 deposits (one closed), 1 loan, 1 card, 1 investment

    by_id = {holding.account.account_id: holding for holding in holdings}

    deposit = by_id[seed_data.DEPOSIT_ACCOUNT_ID]
    assert isinstance(deposit.detail, Deposit)
    assert deposit.detail.product_type is DepositProductType.CHECKING
    assert deposit.account.balance_cents == Cents(450_000)
    assert deposit.account.interest_rate_bps == Bps(10)

    loan = by_id[seed_data.LOAN_ACCOUNT_ID]
    assert isinstance(loan.detail, Loan)
    assert loan.detail.loan_type is LoanType.MORTGAGE
    assert loan.detail.loan_number == "LN-4471"
    assert loan.detail.original_amount_cents == Cents(3_200_000)
    assert loan.detail.monthly_emi_cents == Cents(19_700)
    assert loan.detail.collateral_asset_id == seed_data.PROPERTY_ASSET_ID
    assert loan.detail.loan_end_date == date(2049, 5, 20)

    card = by_id[seed_data.CARD_ACCOUNT_ID]
    assert isinstance(card.detail, CreditCard)
    assert card.detail.card_last4 == seed_data.CARD_LAST4
    assert card.detail.credit_limit_cents == Cents(500_000)
    assert card.detail.fraud_alerts == 1

    investment = by_id[seed_data.INVESTMENT_ACCOUNT_ID]
    assert isinstance(investment.detail, Investment)
    assert investment.detail.investment_risk_profile is InvestmentRiskProfile.MODERATE
    assert investment.detail.portfolio_value_cents == Cents(1_200_000)
    assert investment.detail.asset_allocation is not None


def test_the_full_pan_is_never_read(repos: Repositories) -> None:
    """Design §4.3 stores ``card_last4`` separately so hot paths never load a full PAN.

    The PAN is in the database — the seed put it there so the ``card_last4`` constraint has
    something
    to check — so this asserts the *repository projection* excludes it, which is the control that
    matters. A field that is never selected cannot leak through a masking rule nobody wrote.
    """
    holdings = repos.financial.list_holdings(PRIMARY_ID, account_types=[AccountType.CARD])
    card = holdings[0].detail
    assert isinstance(card, CreditCard)

    serialized = card.model_dump_json()
    assert seed_data.CARD_PAN not in serialized
    assert "card_number" not in serialized
    assert seed_data.CARD_LAST4 in serialized
    assert not hasattr(card, "card_number")


def test_holdings_filters_are_applied_in_sql(repos: Repositories, metrics: QueryMetrics) -> None:
    active = repos.financial.list_holdings(PRIMARY_ID, statuses=[AccountStatus.ACTIVE])
    assert len(active) == 5
    assert all(holding.account.account_status is AccountStatus.ACTIVE for holding in active)
    assert seed_data.CLOSED_ACCOUNT_ID not in {h.account.account_id for h in active}

    deposits = repos.financial.list_holdings(PRIMARY_ID, account_types=[AccountType.DEPOSIT])
    assert len(deposits) == 3
    # Only the deposit specialization is fetched, not all four.
    assert metrics.statements[-2:] == [
        "financial.holdings_accounts",
        "financial.holdings_deposit",
    ]


def test_holdings_statement_id_is_stable_across_filter_combinations(
    repos: Repositories, metrics: QueryMetrics
) -> None:
    """Design §13.4's cardinality rule: one span name per read, not one per filter combination."""
    repos.financial.list_holdings(PRIMARY_ID)
    repos.financial.list_holdings(PRIMARY_ID, account_types=[AccountType.LOAN])
    repos.financial.list_holdings(
        PRIMARY_ID,
        account_types=[AccountType.LOAN, AccountType.CARD],
        statuses=[AccountStatus.ACTIVE],
    )
    assert metrics.statements.count("financial.holdings_accounts") == 3


def test_an_empty_filter_matches_nothing_rather_than_everything(
    repos: Repositories, metrics: QueryMetrics
) -> None:
    """``IN ()`` is a SQLite syntax error, and reading ``[]`` as "no filter" inverts the ask."""
    assert repos.financial.list_holdings(PRIMARY_ID, account_types=[]) == ()
    assert repos.financial.list_holdings(PRIMARY_ID, statuses=[]) == ()
    assert repos.financial.list_transactions(PRIMARY_ID, categories=[]) == ()
    assert repos.journey.list_engagement_events(PRIMARY_ID, channels=[]) == ()
    assert repos.journey.list_engagement_events(PRIMARY_ID, event_types=[]) == ()
    # None of those should have reached the database at all.
    assert metrics.statements == []


def test_holdings_for_a_customer_with_none(repos: Repositories) -> None:
    assert repos.financial.list_holdings(SECONDARY_ID) == ()


def test_transactions_are_newest_first_and_bounded(repos: Repositories) -> None:
    transactions = repos.financial.list_transactions(PRIMARY_ID)
    assert len(transactions) == 5
    dates = [transaction.transaction_date for transaction in transactions]
    assert dates == sorted(dates, reverse=True)
    # `as_of_date` for an event is the date it happened.
    assert all(t.as_of_date == t.transaction_date for t in transactions)

    assert len(repos.financial.list_transactions(PRIMARY_ID, limit=2)) == 2
    with pytest.raises(ValueError, match="limit must be at least 1"):
        repos.financial.list_transactions(PRIMARY_ID, limit=0)


def test_transaction_filters(repos: Repositories) -> None:
    august = repos.financial.list_transactions(
        PRIMARY_ID, start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)
    )
    assert {t.transaction_id for t in august} == {"T-0003", "T-0004", "T-0005"}

    groceries = repos.financial.list_transactions(PRIMARY_ID, categories=["GROCERIES"])
    assert {t.transaction_id for t in groceries} == {"T-0001", "T-0002", "T-0003"}
    assert all(t.amount_cents < Cents(0) for t in groceries)


def test_category_totals_are_spend_only_and_exact(repos: Repositories) -> None:
    """Requirement 5.8. Credits are excluded, or a paycheque would net against grocery spend."""
    totals = {
        row.transaction_category: row for row in repos.financial.totals_by_category(PRIMARY_ID)
    }
    assert set(totals) == {"GROCERIES", "TRAVEL"}, "SALARY is a credit and must not appear"

    assert totals["GROCERIES"].total_cents == Cents(seed_data.GROCERIES_TOTAL_CENTS)
    assert totals["GROCERIES"].transaction_count == 3
    assert totals["TRAVEL"].total_cents == Cents(seed_data.TRAVEL_TOTAL_CENTS)
    assert isinstance(totals["TRAVEL"].total_cents, Cents)
    # Returned as positive spend, largest first.
    ordered = [row.transaction_category for row in repos.financial.totals_by_category(PRIMARY_ID)]
    assert ordered == ["TRAVEL", "GROCERIES"]


def test_category_totals_respect_the_date_window(repos: Repositories) -> None:
    july = {
        row.transaction_category: row.total_cents
        for row in repos.financial.totals_by_category(
            PRIMARY_ID, start_date=date(2026, 7, 1), end_date=date(2026, 7, 31)
        )
    }
    assert july == {"GROCERIES": Cents(20_000)}


def test_monthly_totals_are_oldest_first(repos: Repositories) -> None:
    """A trend series reads forwards, even though the query takes the most recent months."""
    months = repos.financial.monthly_totals(PRIMARY_ID)
    assert [row.month for row in months] == ["2026-07", "2026-08"]
    assert months[0].total_cents == Cents(20_000)
    assert months[1].total_cents == Cents(265_000)

    limited = repos.financial.monthly_totals(PRIMARY_ID, months=1)
    assert [row.month for row in limited] == ["2026-08"]

    groceries = repos.financial.monthly_totals(PRIMARY_ID, category="GROCERIES")
    assert [(row.month, int(row.total_cents)) for row in groceries] == [
        ("2026-07", 20_000),
        ("2026-08", 15_000),
    ]
    with pytest.raises(ValueError, match="months must be at least 1"):
        repos.financial.monthly_totals(PRIMARY_ID, months=0)


def test_median_debit(repos: Repositories) -> None:
    assert repos.financial.median_transaction_amount_cents(PRIMARY_ID) == (
        seed_data.MEDIAN_DEBIT_CENTS
    )
    assert repos.financial.median_transaction_amount_cents(SECONDARY_ID) is None


# ================================================================ risk
def test_risk_profile_round_trips(repos: Repositories) -> None:
    profile = repos.risk.get_risk_profile(PRIMARY_ID)
    assert profile is not None
    assert profile.delinquency_status is DelinquencyStatus.DPD_1_29
    assert profile.current_days_past_due == 12
    # INTEGER 0/1 columns become real booleans.
    assert profile.default_indicator is False
    assert profile.chargeoff_indicator is False
    assert profile.aml_flag is False
    assert profile.pep_flag is True
    # Requirement 8.4: a PEP flag forces the non-dismissible compliance indicator.
    assert profile.requires_compliance_indicator


def test_credit_exposure_is_loan_balances_plus_card_limits(repos: Repositories) -> None:
    """Design §4.6 and requirement 8.5. An undrawn limit is still exposure."""
    assert repos.risk.total_credit_exposure_cents(PRIMARY_ID) == (seed_data.CREDIT_EXPOSURE_CENTS)


def test_credit_exposure_is_zero_not_none_without_credit(repos: Repositories) -> None:
    """Zero exposure is a fact; requirement 4.7's "not available" is for missing values."""
    assert repos.risk.total_credit_exposure_cents(SECONDARY_ID) == 0
    assert repos.risk.total_credit_exposure_cents("C-nope") == 0


# ================================================================ relationship
def test_household_members(repos: Repositories) -> None:
    members = repos.relationship.list_household_members(seed_data.HOUSEHOLD_ID)
    assert [(m.customer_id, m.member_role) for m in members] == [
        (PRIMARY_ID, HouseholdRole.HEAD),
        (SECONDARY_ID, HouseholdRole.SPOUSE),
    ]
    assert members[0].joined_date == date(2014, 3, 17)


def test_household_lookup_for_a_customer(repos: Repositories) -> None:
    assert repos.relationship.get_household_id_for_customer(PRIMARY_ID) == seed_data.HOUSEHOLD_ID
    assert repos.relationship.get_household_id_for_customer("C-nope") is None


def test_relationships_are_returned_in_both_directions_unnormalized(
    repos: Repositories,
) -> None:
    """The stored direction is preserved, because half of ``RelationshipType`` is asymmetric."""
    relationships = repos.relationship.list_relationships(PRIMARY_ID)
    assert len(relationships) == 2

    by_id = {relationship.relationship_id: relationship for relationship in relationships}

    outbound = by_id["R-0001"]
    assert outbound.relationship_type is RelationshipType.SPOUSE
    assert outbound.from_customer_id == PRIMARY_ID
    assert outbound.is_inferred is False
    assert outbound.confidence is None
    assert outbound.is_subject(PRIMARY_ID)
    assert outbound.counterparty_of(PRIMARY_ID) == SECONDARY_ID

    # Stored the other way round. The label still reads from SECONDARY, not from PRIMARY: flipping
    # it
    # would turn "Tomas was referred by Renata" into the reverse claim.
    inbound = by_id["R-0002"]
    assert inbound.relationship_type is RelationshipType.REFERRED_BY
    assert inbound.from_customer_id == SECONDARY_ID
    assert inbound.to_customer_id == PRIMARY_ID
    assert not inbound.is_subject(PRIMARY_ID)
    assert inbound.counterparty_of(PRIMARY_ID) == SECONDARY_ID

    # Requirement 6.6: an inferred link carries a confidence score and a basis.
    assert inbound.is_inferred is True
    assert inbound.confidence == pytest.approx(0.72)
    assert inbound.inference_basis == "shared address hash ah-9f2c"

    # Ordered system-of-record before inferred, so the UI's visual distinction has a stable order.
    assert [r.is_inferred for r in relationships] == [False, True]


def test_counterparty_rejects_an_unrelated_customer(repos: Repositories) -> None:
    relationship = repos.relationship.list_relationships(PRIMARY_ID)[0]
    with pytest.raises(ValueError, match="is not a party to"):
        relationship.counterparty_of("C-stranger")


def test_account_parties_and_joint_accounts(repos: Repositories) -> None:
    parties = repos.relationship.list_account_parties(seed_data.DEPOSIT_ACCOUNT_ID)
    assert [(p.customer_id, p.party_role) for p in parties] == [
        (SECONDARY_ID, PartyRole.JOINT),
        (PRIMARY_ID, PartyRole.PRIMARY),
    ]
    assert parties[0].ownership_bps == Bps(5_000)

    # The savings account has a single party, so it is not a joint account even though the customer
    # is a PRIMARY party on it.
    joint = repos.relationship.list_joint_accounts(PRIMARY_ID)
    assert [party.account_id for party in joint] == [seed_data.DEPOSIT_ACCOUNT_ID]


def test_beneficiaries_including_non_customers(repos: Repositories) -> None:
    beneficiaries = repos.relationship.list_beneficiaries(PRIMARY_ID)
    assert [b.beneficiary_id for b in beneficiaries] == ["B-0001", "B-0002"]

    named_customer, external = beneficiaries
    assert named_customer.beneficiary_customer_id == SECONDARY_ID
    assert named_customer.share_bps == Bps(6_000)
    assert named_customer.is_inferred is False

    # A beneficiary who is not a customer of the bank still has to be displayable.
    assert external.beneficiary_customer_id is None
    assert external.beneficiary_name == "Elena Alvarez"
    assert external.is_inferred is True
    assert external.confidence == pytest.approx(0.55)


def test_assets_with_specializations(repos: Repositories) -> None:
    assets = repos.relationship.list_assets(PRIMARY_ID)
    assert len(assets) == 2
    by_id = {holding.asset.asset_id: holding for holding in assets}

    property_holding = by_id[seed_data.PROPERTY_ASSET_ID]
    assert property_holding.asset.asset_type is AssetType.PROPERTY
    assert property_holding.asset.is_collateral is True
    assert property_holding.asset.current_value_cents == Cents(4_100_000)
    assert isinstance(property_holding.detail, PropertyDetail)
    assert property_holding.detail.property_type is PropertyType.PRIMARY_RESIDENCE
    assert property_holding.detail.year_built == 1998

    vehicle_holding = by_id[seed_data.VEHICLE_ASSET_ID]
    assert vehicle_holding.asset.is_collateral is False
    assert isinstance(vehicle_holding.detail, VehicleDetail)
    assert vehicle_holding.detail.make == "Subaru"
    assert vehicle_holding.detail.vin == "4S4BTAFC7M3123456"


def test_assets_for_a_customer_with_none(repos: Repositories) -> None:
    assert repos.relationship.list_assets(SECONDARY_ID) == ()


# ================================================================ offers
def test_offers_are_joined_to_the_customer_reaction(repos: Repositories) -> None:
    offers = repos.offer.list_offers_for_customer(PRIMARY_ID)
    assert len(offers) == 2
    by_offer = {entry.offer.offer_id: entry for entry in offers}

    upsell = by_offer["O-0001"]
    assert upsell.offer.offer_type is OfferType.UPSELL
    assert upsell.offer.business_group is BusinessGroup.WEALTH
    assert upsell.offer.value_cents == Cents(240_000)
    assert upsell.customer_offer.customer_reaction is CustomerReaction.INTERESTED
    assert upsell.customer_offer.acceptance_probability == pytest.approx(0.42)
    assert upsell.customer_offer.reaction_date == date(2026, 8, 7)
    # Requirement 9.7: campaign membership travels with the offer.
    assert upsell.campaign is not None
    assert upsell.campaign.campaign_name == "Autumn Wealth Review"
    assert upsell.as_of_date == date(2026, 9, 1)

    cross_sell = by_offer["O-0002"]
    assert cross_sell.offer.offer_type is OfferType.CROSS_SELL
    assert cross_sell.campaign is None


def test_suppressed_offers_are_returned_by_default(repos: Repositories) -> None:
    """Requirement 9.5 keeps a suppressed offer visible, de-emphasized, with its reason."""
    with_suppressed = repos.offer.list_offers_for_customer(PRIMARY_ID)
    suppressed = next(entry for entry in with_suppressed if entry.customer_offer.is_suppressed)
    assert suppressed.offer.offer_id == "O-0002"
    assert suppressed.customer_offer.suppression_reason is not None
    assert "DUPLICATE_PRODUCT" in suppressed.customer_offer.suppression_reason

    without = repos.offer.list_offers_for_customer(PRIMARY_ID, include_suppressed=False)
    assert [entry.offer.offer_id for entry in without] == ["O-0001"]


def test_offers_are_newest_presentation_first(repos: Repositories) -> None:
    offers = repos.offer.list_offers_for_customer(PRIMARY_ID)
    dates = [entry.customer_offer.event_date for entry in offers]
    assert dates == sorted(dates, reverse=True)


def test_campaigns_for_customer(repos: Repositories) -> None:
    campaigns = repos.offer.list_campaigns_for_customer(PRIMARY_ID)
    assert [campaign.campaign_id for campaign in campaigns] == ["CM-0001"]
    assert repos.offer.list_campaigns_for_customer(SECONDARY_ID) == ()


def test_offers_for_a_customer_with_none(repos: Repositories, metrics: QueryMetrics) -> None:
    assert repos.offer.list_offers_for_customer(SECONDARY_ID) == ()
    # Short-circuits after the first read rather than issuing all three.
    assert metrics.statements == ["offer.customer_offers"]


# ================================================================ journey
def test_life_events_including_the_inferred_one(repos: Repositories) -> None:
    events = repos.journey.list_life_events(PRIMARY_ID)
    assert [event.life_event_id for event in events] == ["LE-0002", "LE-0001"]

    inferred, of_record = events
    assert inferred.life_event_type is LifeEventType.CHILD_BIRTH
    assert inferred.source is LifeEventSource.INFERRED
    assert inferred.is_inferred is True
    assert inferred.confidence == pytest.approx(0.81)
    # Requirement 7.3: the signals behind an inference are cited.
    assert inferred.signals is not None
    assert "PEDIATRIC_CLINIC" in inferred.signals

    assert of_record.source is LifeEventSource.SYSTEM_OF_RECORD
    assert of_record.is_inferred is False
    assert of_record.confidence is None


def test_applications(repos: Repositories) -> None:
    applications = repos.journey.list_applications(PRIMARY_ID)
    assert [a.application_id for a in applications] == ["AP-0002", "AP-0001"]

    pending, funded = applications
    assert pending.application_status is ApplicationStatus.IN_REVIEW
    assert pending.decision_date is None
    assert not pending.application_status.is_decided

    assert funded.application_status is ApplicationStatus.FUNDED
    assert funded.application_status.is_decided
    assert funded.decision_date == date(2018, 2, 9)
    assert funded.approved_amount_cents == Cents(500_000)
    assert funded.account_id == seed_data.CARD_ACCOUNT_ID


def test_engagement_events_are_filterable(repos: Repositories) -> None:
    """Requirement 7.6."""
    every = repos.journey.list_engagement_events(PRIMARY_ID)
    # Newest date first; within a date, event ID ascending as the deterministic tiebreaker. EV-0001
    # and EV-0002 share 2026-08-30, so the ID decides — the column has no time component.
    assert [event.event_id for event in every] == ["EV-0001", "EV-0002", "EV-0003"]

    mobile = repos.journey.list_engagement_events(PRIMARY_ID, channels=[EngagementChannel.MOBILE])
    assert [event.event_id for event in mobile] == ["EV-0001"]

    logins = repos.journey.list_engagement_events(
        PRIMARY_ID, event_types=[EngagementEventType.LOGIN, EngagementEventType.BRANCH_VISIT]
    )
    assert {event.event_id for event in logins} == {"EV-0001", "EV-0003"}

    combined = repos.journey.list_engagement_events(
        PRIMARY_ID,
        channels=[EngagementChannel.BRANCH],
        event_types=[EngagementEventType.BRANCH_VISIT],
    )
    assert [event.event_id for event in combined] == ["EV-0003"]
    assert combined[0].notes is not None

    assert len(repos.journey.list_engagement_events(PRIMARY_ID, limit=1)) == 1
    with pytest.raises(ValueError, match="limit must be at least 1"):
        repos.journey.list_engagement_events(PRIMARY_ID, limit=0)


def test_major_transactions_use_either_criterion(repos: Repositories) -> None:
    """Requirement 7.4: absolute threshold OR a multiple of the customer's median."""
    # A high absolute threshold, with the median multiple doing the work. Median is 15,000; at 10x
    # the bar is 150,000, so only the 250,000 travel debit clears it.
    by_median = repos.journey.list_major_transactions(
        PRIMARY_ID,
        absolute_threshold_cents=100_000_000,
        median_multiple_bps=100_000,
    )
    assert [t.transaction_id for t in by_median] == ["T-0004"]

    # A low absolute threshold with an unreachable multiple: the threshold does the work instead.
    by_threshold = repos.journey.list_major_transactions(
        PRIMARY_ID,
        absolute_threshold_cents=10_000,
        median_multiple_bps=100_000_000,
    )
    assert {t.transaction_id for t in by_threshold} == {"T-0001", "T-0003", "T-0004"}

    # Credits never qualify, or every payday would land on the timeline.
    assert all(t.amount_cents < Cents(0) for t in by_threshold)
    assert "T-0005" not in {t.transaction_id for t in by_threshold}

    # Largest first.
    amounts = [abs(int(t.amount_cents)) for t in by_threshold]
    assert amounts == sorted(amounts, reverse=True)


def test_major_transactions_validate_their_arguments(repos: Repositories) -> None:
    with pytest.raises(ValueError, match="absolute_threshold_cents must be positive"):
        repos.journey.list_major_transactions(
            PRIMARY_ID, absolute_threshold_cents=0, median_multiple_bps=100_000
        )
    with pytest.raises(ValueError, match="median_multiple_bps must be positive"):
        repos.journey.list_major_transactions(
            PRIMARY_ID, absolute_threshold_cents=1, median_multiple_bps=0
        )
    with pytest.raises(ValueError, match="limit must be at least 1"):
        repos.journey.list_major_transactions(
            PRIMARY_ID, absolute_threshold_cents=1, median_multiple_bps=1, limit=0
        )


def test_major_transactions_for_a_customer_with_none(repos: Repositories) -> None:
    assert (
        repos.journey.list_major_transactions(
            SECONDARY_ID, absolute_threshold_cents=1, median_multiple_bps=1
        )
        == ()
    )


# ================================================================ read-only guarantee
def test_the_repository_engine_cannot_write(repos: Repositories) -> None:
    """Design §12.3 opens the customer database read-only, and this is the proof for this path."""
    from sqlalchemy.exc import OperationalError  # noqa: PLC0415

    with (
        pytest.raises(OperationalError, match="readonly database"),
        repos.customer.engine.begin() as connection,
    ):
        connection.execute(text("DELETE FROM customer"))


# ================================================================ instrumentation
def test_repository_reads_emit_spans(
    repos: Repositories, span_exporter: InMemorySpanExporter
) -> None:
    """Phase 1 gate: DB spans visible in traces.

    The exporter sits behind the same allowlist processor production uses, so what is asserted here
    is
    what would really be exported.
    """
    repos.customer.get(PRIMARY_ID)

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "SELECT customer"

    attributes = dict(span.attributes or {})
    assert attributes["db.system.name"] == "sqlite"
    assert attributes["db.operation.name"] == "SELECT"
    assert attributes["db.collection.name"] == "customer"
    assert attributes[SpanAttr.DB_STATEMENT_ID] == "customer.get"
    assert attributes[SpanAttr.DB_ROW_COUNT] == 1
    assert attributes[SpanAttr.OUTCOME] == "ok"
    assert SpanAttr.DB_POOL_WAIT_MS in attributes


def test_spans_carry_a_statement_id_and_never_the_statement(
    repos: Repositories, span_exporter: InMemorySpanExporter
) -> None:
    """Design §13.4 keeps SQL off spans, because bound values ride along with the statement."""
    repos.financial.list_transactions(
        PRIMARY_ID, start_date=date(2026, 8, 1), categories=["GROCERIES"]
    )

    span = span_exporter.get_finished_spans()[0]
    attributes = dict(span.attributes or {})

    assert attributes[SpanAttr.DB_STATEMENT_ID] == "financial.transactions"
    for forbidden in ("db.statement", "db.query.text", "db.query.parameter.customer_id"):
        assert forbidden not in attributes

    # No attribute value anywhere leaks the identifier or the filter values.
    rendered = " ".join(str(value) for value in attributes.values())
    assert PRIMARY_ID not in rendered
    assert "GROCERIES" not in rendered
    assert span.name == "SELECT txn"


def test_span_names_stay_low_cardinality(
    repos: Repositories, span_exporter: InMemorySpanExporter
) -> None:
    for after in (None, PRIMARY_ID, SECONDARY_ID):
        repos.customer.list_ids(limit=5, after=after)
    names = {span.name for span in span_exporter.get_finished_spans()}
    assert names == {"SELECT customer"}


def test_a_failing_read_is_wrapped_and_marked(
    repos: Repositories, span_exporter: InMemorySpanExporter
) -> None:
    """Services and the aggregator catch one exception type without importing SQLAlchemy."""
    broken = Statement(id="test.broken", collection="customer", sql="SELECT nope FROM customer")

    with pytest.raises(RepositoryError, match=re.escape("read test.broken failed")):
        repos.customer.fetch_all(broken)

    span = span_exporter.get_finished_spans()[0]
    attributes = dict(span.attributes or {})
    assert attributes[SpanAttr.OUTCOME] == "error"
    # The failing SQL must not have been attached to the span.
    assert "db.statement" not in attributes


def test_fetch_one_rejects_multiple_matches(repos: Repositories) -> None:
    """Every caller queries by primary key, so more than one row is a bug, not a data condition."""
    ambiguous = Statement(
        id="test.ambiguous", collection="customer", sql="SELECT customer_id FROM customer"
    )
    with pytest.raises(RepositoryError, match="matched 2"):
        repos.customer.fetch_one(ambiguous)


# ================================================================ helpers
def test_expand_in_clause_generates_placeholders_not_values() -> None:
    fragment, bindings = expand_in_clause("kind", ["DEPOSIT", "LOAN"])
    assert fragment == ":kind_0, :kind_1"
    assert bindings == {"kind_0": "DEPOSIT", "kind_1": "LOAN"}
    # The values themselves never appear in the SQL fragment.
    assert "DEPOSIT" not in fragment


def test_expand_in_clause_rejects_an_empty_sequence() -> None:
    with pytest.raises(ValueError, match="at least one value"):
        expand_in_clause("kind", [])


def test_statement_span_name() -> None:
    assert Statement(id="x.y", collection="customer", sql="SELECT 1").span_name == "SELECT customer"


# ================================================================ defensive join paths
# Both branches below exist because of a bug this suite caught: the specialization reads fetch
# every row for the customer, while the parent list may have been narrowed by a filter, so a
# specialization row with no matching parent is expected rather than exceptional. The first version
# of the mapper raised KeyError on it.


def test_a_specialization_without_a_matching_parent_is_skipped(repos: Repositories) -> None:
    """Reproduces the narrowing case: the closed account's deposit row has no parent here."""
    holdings = repos.financial.list_holdings(
        PRIMARY_ID, account_types=[AccountType.DEPOSIT], statuses=[AccountStatus.ACTIVE]
    )
    ids = {holding.account.account_id for holding in holdings}
    assert ids == {seed_data.DEPOSIT_ACCOUNT_ID, seed_data.SAVINGS_ACCOUNT_ID}
    assert seed_data.CLOSED_ACCOUNT_ID not in ids
    # Every returned holding still got its specialization.
    assert all(holding.detail is not None for holding in holdings)


def test_an_asset_specialization_without_a_parent_is_skipped(repos: Repositories) -> None:
    """The same guard on the asset path, exercised through the loader directly.

    The public read has no filter that can narrow the parent list, so the branch is unreachable from
    outside — which is why it is worth pinning rather than deleting: it is the same shape of bug the
    holdings path actually had. The parent list here declares a ``PROPERTY`` asset that does not
    exist, so the property statement runs and returns a row belonging to a different asset.
    """
    real = {
        holding.asset.asset_id: holding.asset
        for holding in repos.relationship.list_assets(PRIMARY_ID)
    }
    phantom = real[seed_data.PROPERTY_ASSET_ID].model_copy(update={"asset_id": "AS-PROP-PHANTOM"})

    details = repos.relationship._load_asset_details(PRIMARY_ID, [phantom])

    # The real property row was fetched, found to have no parent in the list, and skipped — rather
    # than raising KeyError or being attributed to the phantom.
    assert details == {}


def test_a_customer_offer_with_no_matching_offer_is_skipped(repos: Repositories) -> None:
    """One dangling row must not empty the offer module.

    The foreign key makes this unreachable in practice, which is the point of asserting it here
    rather than trusting it: the guard is what keeps a partial read degrading gracefully.
    """
    assert len(repos.offer.list_offers_for_customer(PRIMARY_ID)) == 2

    class OfferRepositoryWithNoCatalogue(SqliteOfferRepository):
        """Resolves no offers, leaving every ``customer_offer`` row dangling."""

        def fetch_all(
            self,
            statement: Statement,
            params: Mapping[str, Any] | None = None,
        ) -> list[Row[Any]]:
            if statement.id == "offer.offers_for_customer":
                return []
            return super().fetch_all(statement, params)

    broken = OfferRepositoryWithNoCatalogue(repos.offer.engine)
    assert broken.list_offers_for_customer(PRIMARY_ID) == ()
