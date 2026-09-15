"""Products and transactions.

Transcribed from design §4.3, with the index inventory from §4.5.

Accounts use a shared parent (``account``) with typed specializations (``deposit``, ``loan``,
``credit_card``, ``investment``). That shape is what lets "all holdings" stay a single query for
requirement 5.3 while each product type keeps constraints that only make sense for it — a maturity
date on a CD, a credit limit on a card. The alternative, one wide table with mostly-NULL columns,
would make every product-specific rule un-enforceable.

Two things to flag:

**``loan.collateral_asset_id`` references a table that does not exist yet.** ``asset`` is created in
migration ``0003``. SQLite resolves foreign key targets at DML time, not at ``CREATE TABLE`` time,
so this is legal and the reference becomes live once ``0003`` applies. The consequence is narrow but
real: between ``0002`` and ``0003`` an insert into ``loan`` with a non-NULL ``collateral_asset_id``
fails with ``no such table``, and ``PRAGMA foreign_key_check`` on ``loan`` errors rather than
returning rows. Nothing loads data at an intermediate revision — the seeder migrates to head first —
and :mod:`tests.test_migrations` asserts ``foreign_key_check`` is clean at head. The alternative was
to pull ``asset`` forward into this migration, which would have put the collateral tables in the
migration named for products and split requirement 6.1's entities across two.

**``card_last4`` is checked against ``card_number``.** Design §4.3 stores the two separately "so hot
paths never load a full PAN". Storing a derivable value twice invites the copies to disagree, and a
last-4 that does not match the PAN would show the wrong card in a fraud conversation, so the
relationship is a constraint rather than a convention.

Date columns use ``IS date(x)`` rather than ``= date(x)``; migration ``0001`` explains why.

Revision ID: 0002_products_and_transactions
Revises: 0001_core_customer_tables
Created: 2026-09-13
"""

from __future__ import annotations

from c360.data.ddl import Statements, drop_tables, execute_all

revision: str = "0002_products_and_transactions"
down_revision: str | None = "0001_core_customer_tables"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE: Statements = (
    # ------------------------------------------------------------ account
    """
    CREATE TABLE account (
      account_id              TEXT PRIMARY KEY,
      customer_id             TEXT NOT NULL REFERENCES customer(customer_id),
      account_number          TEXT NOT NULL,
      account_type            TEXT NOT NULL CHECK (account_type IN
                                ('DEPOSIT','LOAN','CARD','INVESTMENT')),
      product_name            TEXT,
      product_code            TEXT,
      balance_cents           INTEGER NOT NULL DEFAULT 0,
      available_balance_cents INTEGER,
      interest_rate_bps       INTEGER,
      account_status          TEXT NOT NULL CHECK (account_status IN
                                ('ACTIVE','DORMANT','CLOSED','FROZEN')),
      open_date               TEXT NOT NULL CHECK (open_date IS date(open_date)),
      close_date              TEXT CHECK (close_date IS NULL OR close_date IS date(close_date)),
      as_of_date              TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (close_date IS NULL OR close_date >= open_date),
      -- A closed account has a close date and an open one does not. Requirement 5.3 lists both
      -- fields; a CLOSED row with no close date reads as a data gap rather than a closure.
      CHECK ((account_status = 'CLOSED') = (close_date IS NOT NULL))
    )
    """,
    # Holdings fetch (design §4.5). Ordered so the leading equality on customer_id serves the
    # common "everything this customer holds" read and the trailing columns serve the filtered one.
    "CREATE INDEX ix_account_cust_type ON account(customer_id, account_type, account_status)",
    # Direct ID lookup: search by account number (requirement 3.1).
    "CREATE INDEX ix_account_number ON account(account_number)",
    # ------------------------------------------------------------ deposit
    """
    CREATE TABLE deposit (
      account_id               TEXT PRIMARY KEY REFERENCES account(account_id),
      product_type             TEXT CHECK (product_type IN ('CHECKING','SAVINGS','MMA','CD')),
      household_deposits_cents INTEGER,
      maturity_date            TEXT CHECK (maturity_date IS NULL OR
                                           maturity_date IS date(maturity_date))
    )
    """,
    # ------------------------------------------------------------ loan
    """
    CREATE TABLE loan (
      account_id            TEXT PRIMARY KEY REFERENCES account(account_id),
      loan_number           TEXT NOT NULL,
      loan_type             TEXT CHECK (loan_type IN
                              ('MORTGAGE','AUTO','PERSONAL','HELOC','STUDENT','BUSINESS')),
      original_amount_cents INTEGER NOT NULL CHECK (original_amount_cents > 0),
      monthly_emi_cents     INTEGER CHECK (monthly_emi_cents IS NULL OR monthly_emi_cents >= 0),
      loan_status           TEXT,
      loan_start_date       TEXT CHECK (loan_start_date IS NULL OR
                                        loan_start_date IS date(loan_start_date)),
      loan_end_date         TEXT CHECK (loan_end_date IS NULL OR
                                        loan_end_date IS date(loan_end_date)),
      collateral_asset_id   TEXT REFERENCES asset(asset_id),
      CHECK (loan_end_date IS NULL OR loan_start_date IS NULL OR loan_end_date >= loan_start_date)
    )
    """,
    # Direct ID lookup: search by loan number (requirement 3.1).
    "CREATE INDEX ix_loan_number ON loan(loan_number)",
    # ------------------------------------------------------------ credit_card
    """
    CREATE TABLE credit_card (
      account_id            TEXT PRIMARY KEY REFERENCES account(account_id),
      card_number           TEXT NOT NULL,
      card_last4            TEXT NOT NULL CHECK (length(card_last4) = 4),
      card_type             TEXT,
      credit_limit_cents    INTEGER NOT NULL CHECK (credit_limit_cents > 0),
      utilization_bps       INTEGER CHECK (utilization_bps IS NULL OR utilization_bps >= 0),
      rewards_balance_cents INTEGER,
      monthly_spend_cents   INTEGER,
      overlimit_events      INTEGER NOT NULL DEFAULT 0 CHECK (overlimit_events >= 0),
      fraud_alerts          INTEGER NOT NULL DEFAULT 0 CHECK (fraud_alerts >= 0),
      CHECK (card_last4 = substr(card_number, -4))
    )
    """,
    # Direct ID lookup: search by card last-4 (requirement 3.1).
    "CREATE INDEX ix_card_last4 ON credit_card(card_last4)",
    # ------------------------------------------------------------ investment
    """
    CREATE TABLE investment (
      account_id                TEXT PRIMARY KEY REFERENCES account(account_id),
      portfolio_value_cents     INTEGER NOT NULL DEFAULT 0,
      asset_allocation          TEXT CHECK (asset_allocation IS NULL OR
                                            json_valid(asset_allocation)),
      mutual_funds_cents        INTEGER,
      stocks_cents              INTEGER,
      bonds_cents               INTEGER,
      retirement_accounts_cents INTEGER,
      investment_risk_profile   TEXT CHECK (investment_risk_profile IN
                                  ('CONSERVATIVE','MODERATE','GROWTH','AGGRESSIVE'))
    )
    """,
    # ------------------------------------------------------------ txn
    # The volume table: ~200 rows per customer over 24 months, so ~20k at the default seed and
    # ~200k at 1,000 customers. SQLite has no partitioning; the three composite indexes below are
    # what keep expense analytics and timeline assembly inside the §12 budgets.
    #
    # `customer_id` is denormalized here rather than reached through `account`. It is a deliberate
    # redundancy: every analytics query in requirement 5.8/5.9 filters by customer and date, and
    # joining through `account` first would cost an extra index lookup per row on the largest table
    # in the schema. The foreign key on both columns keeps the redundancy honest.
    """
    CREATE TABLE txn (
      transaction_id       TEXT PRIMARY KEY,
      account_id           TEXT NOT NULL REFERENCES account(account_id),
      customer_id          TEXT NOT NULL REFERENCES customer(customer_id),
      transaction_date     TEXT NOT NULL CHECK (transaction_date IS date(transaction_date)),
      amount_cents         INTEGER NOT NULL,
      transaction_type     TEXT NOT NULL,
      transaction_category TEXT NOT NULL,
      merchant             TEXT,
      channel              TEXT,
      status               TEXT NOT NULL
    )
    """,
    # Design §4.3 gives these three verbatim. DESC on the date columns matters: the reads are
    # "most recent first" and a DESC index removes the sort from the plan.
    "CREATE INDEX ix_txn_cust_date ON txn(customer_id, transaction_date DESC)",
    "CREATE INDEX ix_txn_cust_cat  ON txn(customer_id, transaction_category, transaction_date)",
    "CREATE INDEX ix_txn_acct_date ON txn(account_id, transaction_date DESC)",
)

_DROP_ORDER: Statements = (
    "txn",
    "investment",
    "credit_card",
    "loan",
    "deposit",
    "account",
)


def upgrade() -> None:
    execute_all(_UPGRADE)


def downgrade() -> None:
    drop_tables(_DROP_ORDER)
