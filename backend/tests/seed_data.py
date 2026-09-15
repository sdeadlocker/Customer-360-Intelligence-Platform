"""A small hand-written dataset for the repository tests (task 1.6).

Deliberately hand-written rather than produced by the Phase 2 generator. The Phase 1 gate says
"repositories round-trip hand-seeded rows", and it says so for a reason: if these tests ran against
the
generator, a generator bug and a repository bug would be indistinguishable, and Phase 2 could not
use
these repositories to check itself.

Every row here also has to satisfy the ``CHECK`` constraints from the migrations, so the module
doubles
as a worked example of a valid record — including the awkward pairings the constraints enforce
(``is_inferred`` with ``confidence``, a decided application with a ``decision_date``, ``card_last4``
agreeing with the PAN).

Two customers, because a single customer cannot exercise a household, a relationship or a joint
account. ``C2`` is intentionally sparser than ``C1``: it has no credit profile, no assets and no
offers, which is what makes the "returns ``None`` rather than raising" paths real.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import Connection, text

#: The customer the assertions are written against.
PRIMARY_ID: Final = "C-0001"
#: Household member and relationship counterparty. Sparse on purpose.
SECONDARY_ID: Final = "C-0002"

HOUSEHOLD_ID: Final = "H-0001"
EMPLOYER_ID: Final = "E-0001"

DEPOSIT_ACCOUNT_ID: Final = "A-DEP-1"
SAVINGS_ACCOUNT_ID: Final = "A-DEP-2"
LOAN_ACCOUNT_ID: Final = "A-LOAN-1"
CARD_ACCOUNT_ID: Final = "A-CARD-1"
INVESTMENT_ACCOUNT_ID: Final = "A-INV-1"
CLOSED_ACCOUNT_ID: Final = "A-DEP-CLOSED"

PROPERTY_ASSET_ID: Final = "AS-PROP-1"
VEHICLE_ASSET_ID: Final = "AS-VEH-1"

AS_OF: Final = "2026-09-01"
SOURCE_CORE: Final = "CORE_BANKING"
SOURCE_CRM: Final = "CRM"

#: Full PAN, stored so the ``card_last4`` constraint has something to check against. The repository
#: never selects this column; see :mod:`c360.data.repositories.financial`.
CARD_PAN: Final = "4111111111114242"
CARD_LAST4: Final = "4242"

_STATEMENTS: tuple[str, ...] = (
    # ------------------------------------------------------------ reference
    f"""
    INSERT INTO employer (employer_id, employer_name, industry, city, state)
    VALUES ('{EMPLOYER_ID}', 'Northwind Logistics', 'Transportation', 'Columbus', 'OH')
    """,
    f"""
    INSERT INTO household (household_id, household_name, primary_customer_id, address_hash,
                           member_count, as_of_date)
    VALUES ('{HOUSEHOLD_ID}', 'Alvarez Household', '{PRIMARY_ID}', 'ah-9f2c', 2, '{AS_OF}')
    """,
    # ------------------------------------------------------------ customers
    f"""
    INSERT INTO customer (customer_id, customer_name, customer_type, customer_segment,
                          customer_since, date_of_birth, citizenship, occupation, employer_id,
                          employment_status, marital_status, customer_value, customer_value_score,
                          preferred_language, preferred_channel, household_id, as_of_date,
                          source_system)
    VALUES ('{PRIMARY_ID}', 'Renata Alvarez', 'INDIVIDUAL', 'AFFLUENT', '2014-03-17', '1982-11-04',
            'US', 'Operations Manager', '{EMPLOYER_ID}', 'EMPLOYED', 'MARRIED', 'GOLD', 78.5,
            'en', 'MOBILE', '{HOUSEHOLD_ID}', '{AS_OF}', '{SOURCE_CORE}')
    """,
    # Sparse by design: the thin-file shape. No DOB, occupation, employer or preferred channel.
    f"""
    INSERT INTO customer (customer_id, customer_name, customer_type, customer_segment,
                          customer_since, customer_value, preferred_language, household_id,
                          as_of_date, source_system)
    VALUES ('{SECONDARY_ID}', 'Tomas Alvarez', 'INDIVIDUAL', 'MASS', '2019-08-02', 'BRONZE',
            'es', '{HOUSEHOLD_ID}', '{AS_OF}', '{SOURCE_CRM}')
    """,
    f"""
    INSERT INTO contact_info (customer_id, email, phone_number, mobile_number, address_line1,
                              address_line2, city, state, country, postal_code, as_of_date)
    VALUES ('{PRIMARY_ID}', 'renata.alvarez@example.invalid', '+1-614-555-0142',
            '+1-614-555-0199', '4820 Kestrel Lane', 'Apt 12', 'Columbus', 'OH', 'US', '43215',
            '{AS_OF}')
    """,
    # ------------------------------------------------------------ profiles
    # assets 4,325,000 - liabilities 2,585,000 = net worth 1,740,000 cents, satisfying the CHECK.
    f"""
    INSERT INTO financial_profile (customer_id, total_deposits_cents, total_loans_cents,
                                   total_investments_cents, total_assets_cents,
                                   total_liabilities_cents, net_worth_cents,
                                   household_net_worth_cents, monthly_income_cents,
                                   monthly_expense_cents, as_of_date)
    VALUES ('{PRIMARY_ID}', 1850000, 2500000, 1200000, 4325000, 2585000, 1740000, 1955000,
            920000, 610000, '{AS_OF}')
    """,
    f"""
    INSERT INTO financial_profile (customer_id, total_assets_cents, total_liabilities_cents,
                                   net_worth_cents, as_of_date)
    VALUES ('{SECONDARY_ID}', 215000, 0, 215000, '{AS_OF}')
    """,
    f"""
    INSERT INTO credit_profile (customer_id, fico_score, behavior_score, propensity_score,
                                credit_utilization_bps, years_on_bureau, num_inquiries, num_trades,
                                num_credit_accounts, credit_exposure_cents, as_of_date)
    VALUES ('{PRIMARY_ID}', 742, 610, 63.25, 1700, 14.5, 2, 9, 5, 3500000, '{AS_OF}')
    """,
    f"""
    INSERT INTO risk_profile (customer_id, risk_score, fraud_score, pid_score, sid_score,
                              delinquency_status, current_days_past_due, default_indicator,
                              chargeoff_indicator, aml_flag, pep_flag, as_of_date)
    VALUES ('{PRIMARY_ID}', 31.5, 12.0, 8.25, 4.5, 'DPD_1_29', 12, 0, 0, 0, 1, '{AS_OF}')
    """,
    # ------------------------------------------------------------ accounts
    f"""
    INSERT INTO account (account_id, customer_id, account_number, account_type, product_name,
                         product_code, balance_cents, available_balance_cents, interest_rate_bps,
                         account_status, open_date, as_of_date)
    VALUES ('{DEPOSIT_ACCOUNT_ID}', '{PRIMARY_ID}', '100200300', 'DEPOSIT', 'Everyday Checking',
            'DDA-STD', 450000, 445000, 10, 'ACTIVE', '2014-03-17', '{AS_OF}')
    """,
    f"""
    INSERT INTO account (account_id, customer_id, account_number, account_type, product_name,
                         product_code, balance_cents, interest_rate_bps, account_status,
                         open_date, as_of_date)
    VALUES ('{SAVINGS_ACCOUNT_ID}', '{PRIMARY_ID}', '100200301', 'DEPOSIT', 'High Yield Savings',
            'SAV-HY', 1400000, 415, 'ACTIVE', '2016-06-01', '{AS_OF}')
    """,
    f"""
    INSERT INTO account (account_id, customer_id, account_number, account_type, product_name,
                         product_code, balance_cents, interest_rate_bps, account_status,
                         open_date, as_of_date)
    VALUES ('{LOAN_ACCOUNT_ID}', '{PRIMARY_ID}', '700800900', 'LOAN', '30-Year Fixed Mortgage',
            'MTG-30F', 2500000, 625, 'ACTIVE', '2019-05-20', '{AS_OF}')
    """,
    f"""
    INSERT INTO account (account_id, customer_id, account_number, account_type, product_name,
                         product_code, balance_cents, interest_rate_bps, account_status,
                         open_date, as_of_date)
    VALUES ('{CARD_ACCOUNT_ID}', '{PRIMARY_ID}', '500600700', 'CARD', 'Rewards Platinum Card',
            'CC-PLAT', 85000, 1899, 'ACTIVE', '2018-02-11', '{AS_OF}')
    """,
    f"""
    INSERT INTO account (account_id, customer_id, account_number, account_type, product_name,
                         product_code, balance_cents, account_status, open_date, as_of_date)
    VALUES ('{INVESTMENT_ACCOUNT_ID}', '{PRIMARY_ID}', '900100200', 'INVESTMENT',
            'Managed Portfolio', 'INV-MGD', 1200000, 'ACTIVE', '2017-09-30', '{AS_OF}')
    """,
    # A closed account, so status filtering has something to exclude.
    f"""
    INSERT INTO account (account_id, customer_id, account_number, account_type, product_name,
                         balance_cents, account_status, open_date, close_date, as_of_date)
    VALUES ('{CLOSED_ACCOUNT_ID}', '{PRIMARY_ID}', '100200302', 'DEPOSIT', 'Legacy Checking',
            0, 'CLOSED', '2010-01-04', '2015-07-19', '{AS_OF}')
    """,
    # ------------------------------------------------------------ assets
    # Before the specializations, because `loan.collateral_asset_id` points at the property and
    # `foreign_keys = ON` means the parent has to exist first. Insert order in a real load is the
    # seeder's problem too (task 2.8), so hitting it here is useful rather than annoying.
    f"""
    INSERT INTO asset (asset_id, customer_id, asset_type, asset_description, current_value_cents,
                       ownership_type, acquired_date, is_collateral, as_of_date, source_system)
    VALUES ('{PROPERTY_ASSET_ID}', '{PRIMARY_ID}', 'PROPERTY', 'Primary residence', 4100000,
            'JOINT', '2019-05-20', 1, '{AS_OF}', 'COLLATERAL_SYS')
    """,
    f"""
    INSERT INTO property (asset_id, property_type, address_line1, city, state, postal_code,
                          purchase_price_cents, assessed_value_cents, square_feet, year_built)
    VALUES ('{PROPERTY_ASSET_ID}', 'PRIMARY_RESIDENCE', '4820 Kestrel Lane', 'Columbus', 'OH',
            '43215', 3400000, 4100000, 2150, 1998)
    """,
    f"""
    INSERT INTO asset (asset_id, customer_id, asset_type, asset_description, current_value_cents,
                       ownership_type, acquired_date, is_collateral, as_of_date, source_system)
    VALUES ('{VEHICLE_ASSET_ID}', '{PRIMARY_ID}', 'VEHICLE', '2021 Subaru Outback', 285000,
            'SOLE', '2021-04-02', 0, '{AS_OF}', 'COLLATERAL_SYS')
    """,
    f"""
    INSERT INTO vehicle (asset_id, make, model, model_year, vin, mileage, purchase_price_cents)
    VALUES ('{VEHICLE_ASSET_ID}', 'Subaru', 'Outback', 2021, '4S4BTAFC7M3123456', 41200, 3450000)
    """,
    # ------------------------------------------------------------ specializations
    f"""
    INSERT INTO deposit (account_id, product_type, household_deposits_cents, maturity_date)
    VALUES ('{DEPOSIT_ACCOUNT_ID}', 'CHECKING', 1850000, NULL)
    """,
    f"""
    INSERT INTO deposit (account_id, product_type, household_deposits_cents)
    VALUES ('{SAVINGS_ACCOUNT_ID}', 'SAVINGS', 1850000)
    """,
    f"""
    INSERT INTO deposit (account_id, product_type) VALUES ('{CLOSED_ACCOUNT_ID}', 'CHECKING')
    """,
    f"""
    INSERT INTO loan (account_id, loan_number, loan_type, original_amount_cents, monthly_emi_cents,
                      loan_status, loan_start_date, loan_end_date, collateral_asset_id)
    VALUES ('{LOAN_ACCOUNT_ID}', 'LN-4471', 'MORTGAGE', 3200000, 19700, 'CURRENT', '2019-05-20',
            '2049-05-20', '{PROPERTY_ASSET_ID}')
    """,
    f"""
    INSERT INTO credit_card (account_id, card_number, card_last4, card_type, credit_limit_cents,
                             utilization_bps, rewards_balance_cents, monthly_spend_cents,
                             overlimit_events, fraud_alerts)
    VALUES ('{CARD_ACCOUNT_ID}', '{CARD_PAN}', '{CARD_LAST4}', 'VISA_PLATINUM', 500000, 1700,
            12500, 96000, 0, 1)
    """,
    f"""
    INSERT INTO investment (account_id, portfolio_value_cents, asset_allocation, mutual_funds_cents,
                            stocks_cents, bonds_cents, retirement_accounts_cents,
                            investment_risk_profile)
    VALUES ('{INVESTMENT_ACCOUNT_ID}', 1200000,
            '{{"equity": 0.6, "fixed_income": 0.3, "cash": 0.1}}',
            400000, 500000, 200000, 100000, 'MODERATE')
    """,
    # ------------------------------------------------------------ relationships
    f"""
    INSERT INTO household_member (household_id, customer_id, member_role, joined_date, as_of_date)
    VALUES ('{HOUSEHOLD_ID}', '{PRIMARY_ID}', 'HEAD', '2014-03-17', '{AS_OF}')
    """,
    f"""
    INSERT INTO household_member (household_id, customer_id, member_role, joined_date, as_of_date)
    VALUES ('{HOUSEHOLD_ID}', '{SECONDARY_ID}', 'SPOUSE', '2019-08-02', '{AS_OF}')
    """,
    # System-of-record: no confidence, per the CHECK.
    f"""
    INSERT INTO customer_relationship (relationship_id, from_customer_id, to_customer_id,
                                       relationship_type, is_inferred, source_system, as_of_date)
    VALUES ('R-0001', '{PRIMARY_ID}', '{SECONDARY_ID}', 'SPOUSE', 0, '{SOURCE_CORE}', '{AS_OF}')
    """,
    # Inferred, stored in the *reverse* direction, so the "do not normalize the label" behaviour has
    # something to demonstrate.
    f"""
    INSERT INTO customer_relationship (relationship_id, from_customer_id, to_customer_id,
                                       relationship_type, is_inferred, confidence, inference_basis,
                                       source_system, as_of_date)
    VALUES ('R-0002', '{SECONDARY_ID}', '{PRIMARY_ID}', 'REFERRED_BY', 1, 0.72,
            'shared address hash ah-9f2c', 'GRAPH_INFERENCE', '{AS_OF}')
    """,
    f"""
    INSERT INTO account_party (account_id, customer_id, party_role, ownership_bps, added_date,
                              as_of_date)
    VALUES ('{DEPOSIT_ACCOUNT_ID}', '{PRIMARY_ID}', 'PRIMARY', 5000, '2014-03-17', '{AS_OF}')
    """,
    f"""
    INSERT INTO account_party (account_id, customer_id, party_role, ownership_bps, added_date,
                              as_of_date)
    VALUES ('{DEPOSIT_ACCOUNT_ID}', '{SECONDARY_ID}', 'JOINT', 5000, '2019-08-02', '{AS_OF}')
    """,
    # Sole holder on the savings account: this one must NOT come back as a joint account.
    f"""
    INSERT INTO account_party (account_id, customer_id, party_role, ownership_bps, as_of_date)
    VALUES ('{SAVINGS_ACCOUNT_ID}', '{PRIMARY_ID}', 'PRIMARY', 10000, '{AS_OF}')
    """,
    f"""
    INSERT INTO beneficiary (beneficiary_id, account_id, beneficiary_customer_id, beneficiary_name,
                             relationship, share_bps, is_inferred, confidence, as_of_date)
    VALUES ('B-0001', '{INVESTMENT_ACCOUNT_ID}', '{SECONDARY_ID}', 'Tomas Alvarez', 'SPOUSE',
            6000, 0, NULL, '{AS_OF}')
    """,
    # Not a customer of the bank, and inferred, so it carries a confidence score.
    f"""
    INSERT INTO beneficiary (beneficiary_id, account_id, beneficiary_customer_id, beneficiary_name,
                             relationship, share_bps, is_inferred, confidence, as_of_date)
    VALUES ('B-0002', '{INVESTMENT_ACCOUNT_ID}', NULL, 'Elena Alvarez', 'CHILD', 4000, 1, 0.55,
            '{AS_OF}')
    """,
    # ------------------------------------------------------------ journey
    f"""
    INSERT INTO life_event (life_event_id, customer_id, life_event_type, event_date, confidence,
                            source, is_inferred, signals, as_of_date)
    VALUES ('LE-0001', '{PRIMARY_ID}', 'HOME_PURCHASE', '2019-05-20', NULL, 'SYSTEM_OF_RECORD', 0,
            NULL, '{AS_OF}')
    """,
    f"""
    INSERT INTO life_event (life_event_id, customer_id, life_event_type, event_date, confidence,
                            source, is_inferred, signals, as_of_date)
    VALUES ('LE-0002', '{PRIMARY_ID}', 'CHILD_BIRTH', '2022-02-14', 0.81, 'INFERRED', 1,
            '["txn:PEDIATRIC_CLINIC", "txn:BABY_RETAIL"]', '{AS_OF}')
    """,
    f"""
    INSERT INTO application (application_id, customer_id, product_applied, product_code, event_date,
                             channel, application_status, fraud_result, decision_date,
                             requested_amount_cents, approved_amount_cents, account_id, as_of_date,
                             source_system)
    VALUES ('AP-0001', '{PRIMARY_ID}', 'Rewards Platinum Card', 'CC-PLAT', '2018-02-04', 'WEB',
            'FUNDED', 'PASS', '2018-02-09', 500000, 500000, '{CARD_ACCOUNT_ID}', '{AS_OF}',
            'ORIGINATION')
    """,
    f"""
    INSERT INTO application (application_id, customer_id, product_applied, event_date, channel,
                             application_status, as_of_date, source_system)
    VALUES ('AP-0002', '{PRIMARY_ID}', 'Personal Line of Credit', '2026-08-20', 'MOBILE',
            'IN_REVIEW', '{AS_OF}', 'ORIGINATION')
    """,
    f"""
    INSERT INTO customer_event (event_id, customer_id, event_type, event_date, channel, session_id,
                                device_type, outcome, notes, as_of_date)
    VALUES ('EV-0001', '{PRIMARY_ID}', 'LOGIN', '2026-08-30', 'MOBILE', 'sess-a1', 'iOS',
            'SUCCESS', NULL, '{AS_OF}')
    """,
    f"""
    INSERT INTO customer_event (event_id, customer_id, event_type, event_date, channel, session_id,
                                device_type, outcome, as_of_date)
    VALUES ('EV-0002', '{PRIMARY_ID}', 'OTP_VERIFY', '2026-08-30', 'SMS', 'sess-a1', 'iOS',
            'FAILURE', '{AS_OF}')
    """,
    f"""
    INSERT INTO customer_event (event_id, customer_id, event_type, event_date, channel, outcome,
                                notes, as_of_date)
    VALUES ('EV-0003', '{PRIMARY_ID}', 'BRANCH_VISIT', '2026-07-11', 'BRANCH', 'SUCCESS',
            'Discussed mortgage refinance options.', '{AS_OF}')
    """,
    # ------------------------------------------------------------ offers
    f"""
    INSERT INTO campaign (campaign_id, campaign_name, business_group, channel, start_date, end_date,
                          campaign_status, as_of_date)
    VALUES ('CM-0001', 'Autumn Wealth Review', 'WEALTH', 'EMAIL', '2026-08-01', '2026-10-31',
            'ACTIVE', '{AS_OF}')
    """,
    f"""
    INSERT INTO offer (offer_id, offer_name, business_group, offer_type, product_code, product_type,
                       value_cents, start_date, end_date, offer_status, campaign_id, as_of_date)
    VALUES ('O-0001', 'Managed Portfolio Upgrade', 'WEALTH', 'UPSELL', 'INV-PREM', 'INVESTMENT',
            240000, '2026-08-01', '2026-10-31', 'ACTIVE', 'CM-0001', '{AS_OF}')
    """,
    f"""
    INSERT INTO offer (offer_id, offer_name, business_group, offer_type, product_code, product_type,
                       value_cents, start_date, offer_status, as_of_date)
    VALUES ('O-0002', 'High Yield Savings', 'DEPOSITS', 'CROSS_SELL', 'SAV-HY', 'SAVINGS',
            60000, '2026-06-01', 'ACTIVE', '{AS_OF}')
    """,
    f"""
    INSERT INTO customer_offer (customer_offer_id, customer_id, offer_id, event_date,
                                customer_reaction, reaction_date, acceptance_probability,
                                probability_confidence, expected_value_cents, affinity_basis,
                                is_suppressed, suppression_reason, channel, as_of_date)
    VALUES ('CO-0001', '{PRIMARY_ID}', 'O-0001', '2026-08-05', 'INTERESTED', '2026-08-07', 0.42,
            'MEDIUM', 100800, 'holds a managed portfolio', 0, NULL, 'EMAIL', '{AS_OF}')
    """,
    # Suppressed as a duplicate: the customer already holds a high yield savings account.
    # Requirement
    # 9.5 keeps it visible with its reason, so the read must return it by default.
    f"""
    INSERT INTO customer_offer (customer_offer_id, customer_id, offer_id, event_date,
                                customer_reaction, acceptance_probability, probability_confidence,
                                expected_value_cents, is_suppressed, suppression_reason,
                                as_of_date)
    VALUES ('CO-0002', '{PRIMARY_ID}', 'O-0002', '2026-06-10', 'PENDING', 0.18, 'LOW', 10800, 1,
            'DUPLICATE_PRODUCT: customer already holds SAV-HY', '{AS_OF}')
    """,
)


def _transaction_statements() -> tuple[str, ...]:
    """Transactions across two months, two categories and both signs.

    Chosen so every aggregate has a hand-checkable expected value:

    * GROCERIES: 12,000 + 8,000 in July, 15,000 in August → 35,000 cents over 3 transactions.
    * TRAVEL: 250,000 in August, the only major transaction.
    * SALARY: a credit, which must be excluded from every spend aggregate.

    Absolute debit amounts are 8,000, 12,000, 15,000 and 250,000, so the lower median of four values
    is 15,000 — the third element, at offset ``4 // 2 == 2``.
    """
    rows = [
        ("T-0001", DEPOSIT_ACCOUNT_ID, "2026-07-03", -12_000, "DEBIT", "GROCERIES", "Kroger"),
        ("T-0002", DEPOSIT_ACCOUNT_ID, "2026-07-19", -8_000, "DEBIT", "GROCERIES", "Aldi"),
        ("T-0003", DEPOSIT_ACCOUNT_ID, "2026-08-02", -15_000, "DEBIT", "GROCERIES", "Whole Foods"),
        ("T-0004", CARD_ACCOUNT_ID, "2026-08-14", -250_000, "DEBIT", "TRAVEL", "Delta Air Lines"),
        ("T-0005", DEPOSIT_ACCOUNT_ID, "2026-08-01", 920_000, "CREDIT", "SALARY", "Northwind"),
    ]
    return tuple(f"""
        INSERT INTO txn (transaction_id, account_id, customer_id, transaction_date, amount_cents,
                         transaction_type, transaction_category, merchant, channel, status)
        VALUES ('{txn_id}', '{account_id}', '{PRIMARY_ID}', '{txn_date}', {amount},
                '{txn_type}', '{category}', '{merchant}', 'ONLINE', 'POSTED')
        """ for txn_id, account_id, txn_date, amount, txn_type, category, merchant in rows)


#: Hand-checkable expectations, asserted by the repository tests.
GROCERIES_TOTAL_CENTS: Final = 35_000
TRAVEL_TOTAL_CENTS: Final = 250_000
MEDIAN_DEBIT_CENTS: Final = 15_000
#: Loan balance 2,500,000 + card limit 500,000. The closed account contributes nothing.
CREDIT_EXPOSURE_CENTS: Final = 3_000_000


def seed(connection: Connection) -> None:
    """Insert the whole dataset. Assumes a database migrated to head and a live transaction."""
    for statement in (*_STATEMENTS, *_transaction_statements()):
        connection.execute(text(statement))
