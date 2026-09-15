"""Core customer tables.

Transcribed from design §4.3. The conventions there govern every column: monetary values are
``INTEGER`` cents suffixed ``_cents``, rates and percentages are ``INTEGER`` basis points suffixed
``_bps``, dates are ISO-8601 ``TEXT``, booleans are ``INTEGER 0/1``, and every table carries
``as_of_date`` so requirement 4.8 (show the as-of timestamp for any displayed figure) is answerable
from the row itself rather than from when the query ran.

Date validation: two departures from the design text
----------------------------------------------------

**The check is applied to every date column**, not only the two design §4.3 spells out. Design §1.1
states the general rule that dates are "TEXT ISO-8601 with CHECK constraints", and requirement 14.5
requires records failing schema validation to be rejected, so the rule is applied uniformly here and
in the two migrations that follow.

**The comparison is ``IS date(x)``, not ``= date(x)``.** Design §4.3 writes
``CHECK (customer_since = date(customer_since))``, and that formulation does not do what it appears
to. SQLite's ``date()`` returns NULL for input it cannot parse, ``'2020-1-1' = NULL`` evaluates to
NULL rather than to false, and **a CHECK constraint whose expression is NULL passes**. The literal
design form therefore admits exactly the values most likely to turn up — an unpadded date from a
hand-written fixture, ``'20240115'`` from a system exporting basic format, an empty string — while
correctly rejecting a timestamp, since ``date('2024-01-15T09:00:00')`` parses to ``'2024-01-15'``
and compares unequal. Half-working is the worst case here: the constraint looks like it is holding
the
line, so nothing else checks, and the damage shows up later as a string range query returning the
wrong rows because the column holds mixed widths.

``IS`` is SQLite's NULL-safe equality, so ``'2020-1-1' IS NULL`` is false and the constraint fires.
Combined with ``date()`` normalizing impossible dates (``date('2026-02-30')`` returns
``'2026-03-02'``, which compares unequal), the check now rejects malformed text, non-existent
calendar dates, basic format, week dates and timestamps alike. What survives is exactly
``YYYY-MM-DD`` — which is what :mod:`c360.domain.dates` parses, and what makes string comparison on
these columns sound.

``household.primary_customer_id`` has no foreign key, matching the design. It cannot have one:
``household`` is created before ``customer`` because ``customer.household_id`` points at it, and the
reference is genuinely circular. SQLite has no deferrable constraints to break the cycle with, so
the check belongs to the projection and validation layer instead.

Revision ID: 0001_core_customer_tables
Revises: None
Created: 2026-09-13
"""

from __future__ import annotations

from c360.data.ddl import Statements, drop_tables, execute_all

revision: str = "0001_core_customer_tables"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE: Statements = (
    # ------------------------------------------------------------ employer
    """
    CREATE TABLE employer (
      employer_id   TEXT PRIMARY KEY,
      employer_name TEXT NOT NULL,
      industry      TEXT,
      city          TEXT,
      state         TEXT
    )
    """,
    # ------------------------------------------------------------ household
    # `address_hash` is the basis for derived household grouping (design §4.3). It is a hash rather
    # than the address itself so that grouping never requires loading a maskable field.
    """
    CREATE TABLE household (
      household_id        TEXT PRIMARY KEY,
      household_name      TEXT NOT NULL,
      primary_customer_id TEXT,
      address_hash        TEXT NOT NULL,
      member_count        INTEGER NOT NULL DEFAULT 1 CHECK (member_count >= 1),
      as_of_date          TEXT NOT NULL CHECK (as_of_date IS date(as_of_date))
    )
    """,
    # ------------------------------------------------------------ customer
    """
    CREATE TABLE customer (
      customer_id          TEXT PRIMARY KEY,
      customer_name        TEXT NOT NULL,
      customer_type        TEXT NOT NULL CHECK (customer_type IN
                             ('INDIVIDUAL','JOINT','BUSINESS','TRUST')),
      customer_segment     TEXT NOT NULL CHECK (customer_segment IN
                             ('MASS','AFFLUENT','HNW','UHNW','SMALL_BUSINESS')),
      customer_since       TEXT NOT NULL CHECK (customer_since IS date(customer_since)),
      date_of_birth        TEXT CHECK (date_of_birth IS NULL OR
                                       date_of_birth IS date(date_of_birth)),
      citizenship          TEXT,
      occupation           TEXT,
      employer_id          TEXT REFERENCES employer(employer_id),
      employment_status    TEXT,
      marital_status       TEXT,
      customer_value       TEXT NOT NULL CHECK (customer_value IN
                             ('BRONZE','SILVER','GOLD','PLATINUM')),
      customer_value_score REAL,
      preferred_language   TEXT NOT NULL DEFAULT 'en',
      preferred_channel    TEXT,
      household_id         TEXT REFERENCES household(household_id),
      as_of_date           TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      source_system        TEXT NOT NULL
    )
    """,
    # Household rollups (design §4.5).
    "CREATE INDEX ix_customer_household ON customer(household_id)",
    # Segment-scoped entitlement (SEGMENT(segments), requirement 12.3) filters on this inside the
    # repository query rather than after it, so it needs an index of its own.
    "CREATE INDEX ix_customer_segment ON customer(customer_segment)",
    # ------------------------------------------------------------ contact_info
    """
    CREATE TABLE contact_info (
      customer_id   TEXT PRIMARY KEY REFERENCES customer(customer_id),
      email         TEXT,
      phone_number  TEXT,
      mobile_number TEXT,
      address_line1 TEXT,
      address_line2 TEXT,
      city          TEXT,
      state         TEXT,
      country       TEXT,
      postal_code   TEXT,
      as_of_date    TEXT NOT NULL CHECK (as_of_date IS date(as_of_date))
    )
    """,
    # ------------------------------------------------------------ financial_profile
    # Requirement 5.2: net worth is total assets minus total liabilities. Enforced as a CHECK
    # because the recompute job (task 3.1) and the generator both write these columns, and an
    # inconsistency here would surface as an agent narrating a number that does not add up.
    """
    CREATE TABLE financial_profile (
      customer_id               TEXT PRIMARY KEY REFERENCES customer(customer_id),
      total_deposits_cents      INTEGER NOT NULL DEFAULT 0,
      total_loans_cents         INTEGER NOT NULL DEFAULT 0,
      total_investments_cents   INTEGER NOT NULL DEFAULT 0,
      total_assets_cents        INTEGER NOT NULL DEFAULT 0,
      total_liabilities_cents   INTEGER NOT NULL DEFAULT 0,
      net_worth_cents           INTEGER NOT NULL DEFAULT 0,
      household_net_worth_cents INTEGER,
      monthly_income_cents      INTEGER,
      monthly_expense_cents     INTEGER,
      as_of_date                TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (net_worth_cents = total_assets_cents - total_liabilities_cents)
    )
    """,
    # ------------------------------------------------------------ credit_profile
    """
    CREATE TABLE credit_profile (
      customer_id            TEXT PRIMARY KEY REFERENCES customer(customer_id),
      fico_score             INTEGER CHECK (fico_score IS NULL OR fico_score BETWEEN 300 AND 850),
      behavior_score         INTEGER,
      propensity_score       REAL CHECK (propensity_score IS NULL OR
                                         propensity_score BETWEEN 0 AND 100),
      credit_utilization_bps INTEGER CHECK (credit_utilization_bps IS NULL OR
                                            credit_utilization_bps >= 0),
      years_on_bureau        REAL,
      num_inquiries          INTEGER,
      num_trades             INTEGER,
      num_credit_accounts    INTEGER,
      credit_exposure_cents  INTEGER,
      as_of_date             TEXT NOT NULL CHECK (as_of_date IS date(as_of_date))
    )
    """,
    # ------------------------------------------------------------ risk_profile
    # `current_days_past_due` and `delinquency_status` are two views of one fact, so the bucket
    # boundaries are enforced rather than left to the generator. Requirement 8.1 displays both, and
    # a row saying CURRENT with 45 days past due is the kind of contradiction a risk analyst would
    # rightly stop trusting the platform over.
    """
    CREATE TABLE risk_profile (
      customer_id           TEXT PRIMARY KEY REFERENCES customer(customer_id),
      risk_score            REAL,
      fraud_score           REAL,
      pid_score             REAL,
      sid_score             REAL,
      delinquency_status    TEXT CHECK (delinquency_status IN
                              ('CURRENT','DPD_1_29','DPD_30_59','DPD_60_89','DPD_90_PLUS')),
      current_days_past_due INTEGER NOT NULL DEFAULT 0 CHECK (current_days_past_due >= 0),
      default_indicator     INTEGER NOT NULL DEFAULT 0 CHECK (default_indicator IN (0,1)),
      chargeoff_indicator   INTEGER NOT NULL DEFAULT 0 CHECK (chargeoff_indicator IN (0,1)),
      aml_flag              INTEGER NOT NULL DEFAULT 0 CHECK (aml_flag IN (0,1)),
      pep_flag              INTEGER NOT NULL DEFAULT 0 CHECK (pep_flag IN (0,1)),
      as_of_date            TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (
        delinquency_status IS NULL
        OR (delinquency_status = 'CURRENT'     AND current_days_past_due = 0)
        OR (delinquency_status = 'DPD_1_29'    AND current_days_past_due BETWEEN 1 AND 29)
        OR (delinquency_status = 'DPD_30_59'   AND current_days_past_due BETWEEN 30 AND 59)
        OR (delinquency_status = 'DPD_60_89'   AND current_days_past_due BETWEEN 60 AND 89)
        OR (delinquency_status = 'DPD_90_PLUS' AND current_days_past_due >= 90)
      )
    )
    """,
)

#: Child-before-parent. `customer` references `employer` and `household`, and the four profile
#: tables reference `customer`, so the order is the reverse of creation.
_DROP_ORDER: Statements = (
    "risk_profile",
    "credit_profile",
    "financial_profile",
    "contact_info",
    "customer",
    "household",
    "employer",
)


def upgrade() -> None:
    execute_all(_UPGRADE)


def downgrade() -> None:
    drop_tables(_DROP_ORDER)
