"""Events, offers, campaigns, assets and relationships.

Design §4.3 stops enumerating columns here — "remaining tables follow the same conventions" — and
names the tables only. The column sets below are therefore derived from the acceptance criteria that
read them, and each table notes which ones. That mapping is the point: every column exists because
some requirement displays or filters on it, and a column no requirement asks for is a column the
generator has to invent values for and the masking matrix has to classify.

**The timeline key is uniformly ``event_date``.** Design §4.5 asks for a
``(customer_id, event_date)`` index on ``customer_event``, ``life_event``, ``application`` and
``customer_offer``. Naming the column ``event_date`` on all four — rather than ``application_date``
on one and ``presented_date`` on another — is what lets requirement 7.1's merged timeline be a
``UNION ALL`` over four indexed range scans instead of four differently-shaped queries. Where a
requirement names a second, genuinely distinct date (``decision_date``, ``reaction_date``), that
column exists alongside it.

**``is_inferred`` and ``confidence`` travel together.** Design §4.3 requires them on
``customer_relationship`` and ``beneficiary`` because requirement 6.6 must render an inferred link
differently from a system-of-record one: *a spouse link guessed from a shared address is not the
same fact as a joint account holder*. An inferred row with no confidence score cannot be rendered to
that requirement, so the constraint enforces the pairing rather than trusting the generator.

Date columns use ``IS date(x)`` rather than ``= date(x)``; migration ``0001`` explains why.

Revision ID: 0003_events_offers_assets_relationships
Revises: 0002_products_and_transactions
Created: 2026-09-13
"""

from __future__ import annotations

from c360.data.ddl import Statements, drop_tables, execute_all

revision: str = "0003_events_offers_assets_relationships"
down_revision: str | None = "0002_products_and_transactions"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE: Statements = (
    # ================================================================ assets
    # Requirement 6.1 (linked assets) and design §4.6 (asset current values contribute to
    # total_assets_cents). Created first because `loan.collateral_asset_id`, declared in migration
    # 0002, points here.
    """
    CREATE TABLE asset (
      asset_id            TEXT PRIMARY KEY,
      customer_id         TEXT NOT NULL REFERENCES customer(customer_id),
      asset_type          TEXT NOT NULL CHECK (asset_type IN ('PROPERTY','VEHICLE','OTHER')),
      asset_description   TEXT,
      current_value_cents INTEGER NOT NULL DEFAULT 0 CHECK (current_value_cents >= 0),
      ownership_type      TEXT CHECK (ownership_type IS NULL OR
                            ownership_type IN ('SOLE','JOINT','TRUST')),
      acquired_date       TEXT CHECK (acquired_date IS NULL OR
                                      acquired_date IS date(acquired_date)),
      is_collateral       INTEGER NOT NULL DEFAULT 0 CHECK (is_collateral IN (0,1)),
      as_of_date          TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      source_system       TEXT NOT NULL
    )
    """,
    "CREATE INDEX ix_asset_cust ON asset(customer_id, asset_type)",
    # `property` and `vehicle` specialize `asset` the same way `deposit` and `loan` specialize
    # `account`. `street_address` and `vin` are the two masked fields here (design §7.2), which is
    # why they sit in the specializations rather than on the parent every rollup query touches.
    """
    CREATE TABLE property (
      asset_id             TEXT PRIMARY KEY REFERENCES asset(asset_id),
      property_type        TEXT CHECK (property_type IS NULL OR property_type IN
                             ('PRIMARY_RESIDENCE','SECOND_HOME','INVESTMENT','LAND','COMMERCIAL')),
      address_line1        TEXT,
      city                 TEXT,
      state                TEXT,
      postal_code          TEXT,
      purchase_price_cents INTEGER CHECK (purchase_price_cents IS NULL OR
                                          purchase_price_cents >= 0),
      assessed_value_cents INTEGER CHECK (assessed_value_cents IS NULL OR
                                          assessed_value_cents >= 0),
      square_feet          INTEGER CHECK (square_feet IS NULL OR square_feet > 0),
      year_built           INTEGER CHECK (year_built IS NULL OR year_built BETWEEN 1600 AND 2200)
    )
    """,
    """
    CREATE TABLE vehicle (
      asset_id             TEXT PRIMARY KEY REFERENCES asset(asset_id),
      make                 TEXT,
      model                TEXT,
      model_year           INTEGER CHECK (model_year IS NULL OR model_year BETWEEN 1900 AND 2200),
      vin                  TEXT,
      mileage              INTEGER CHECK (mileage IS NULL OR mileage >= 0),
      purchase_price_cents INTEGER CHECK (purchase_price_cents IS NULL OR
                                          purchase_price_cents >= 0)
    )
    """,
    # ================================================================ applications
    # Requirement 7.5: product applied, application date, channel, status, fraud result and
    # decision date. `event_date` is the application date; see the module docstring.
    """
    CREATE TABLE application (
      application_id         TEXT PRIMARY KEY,
      customer_id            TEXT NOT NULL REFERENCES customer(customer_id),
      product_applied        TEXT NOT NULL,
      product_code           TEXT,
      event_date             TEXT NOT NULL CHECK (event_date IS date(event_date)),
      channel                TEXT,
      application_status     TEXT NOT NULL CHECK (application_status IN
                               ('SUBMITTED','IN_REVIEW','APPROVED','DECLINED',
                                'WITHDRAWN','FUNDED')),
      fraud_result           TEXT CHECK (fraud_result IS NULL OR
                               fraud_result IN ('PASS','REVIEW','FAIL')),
      decision_date          TEXT CHECK (decision_date IS NULL OR
                                         decision_date IS date(decision_date)),
      requested_amount_cents INTEGER CHECK (requested_amount_cents IS NULL OR
                                            requested_amount_cents >= 0),
      approved_amount_cents  INTEGER CHECK (approved_amount_cents IS NULL OR
                                            approved_amount_cents >= 0),
      -- Set once an approved application becomes a funded product, so the journey timeline can
      -- link an application to the holding it produced (requirement 7.1).
      account_id             TEXT REFERENCES account(account_id),
      as_of_date             TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      source_system          TEXT NOT NULL,
      CHECK (decision_date IS NULL OR decision_date >= event_date),
      -- A decided application has a decision date; a pending one does not.
      CHECK ((application_status IN ('APPROVED','DECLINED','FUNDED')) =
             (decision_date IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_application_cust_date ON application(customer_id, event_date)",
    # ================================================================ engagement events
    # Requirement 7.6: event ID, type, date, channel, session ID, device type, outcome — and the
    # history must be filterable by channel and event type, which is what the second index is for.
    """
    CREATE TABLE customer_event (
      event_id    TEXT PRIMARY KEY,
      customer_id TEXT NOT NULL REFERENCES customer(customer_id),
      event_type  TEXT NOT NULL CHECK (event_type IN
                    ('LOGIN','LOGOUT','OTP_REQUEST','OTP_VERIFY','PASSWORD_RESET','BRANCH_VISIT',
                     'APP_USAGE','CALL','CHAT','STATEMENT_VIEW','SERVICE_REQUEST','COMPLAINT')),
      event_date  TEXT NOT NULL CHECK (event_date IS date(event_date)),
      channel     TEXT NOT NULL CHECK (channel IN
                    ('WEB','MOBILE','BRANCH','ATM','CALL_CENTER','EMAIL','SMS')),
      session_id  TEXT,
      device_type TEXT,
      outcome     TEXT CHECK (outcome IS NULL OR
                              outcome IN ('SUCCESS','FAILURE','ABANDONED','PENDING')),
      -- Free text on a service request or complaint. Carried because the Phase 2.7 adversarial
      -- seed lands instruction-like text in exactly this kind of customer-supplied field.
      notes       TEXT,
      as_of_date  TEXT NOT NULL CHECK (as_of_date IS date(as_of_date))
    )
    """,
    "CREATE INDEX ix_customer_event_cust_date ON customer_event(customer_id, event_date)",
    "CREATE INDEX ix_customer_event_filter ON customer_event(customer_id, event_type, channel)",
    # ================================================================ life events
    # Requirement 7.2 (type, date, confidence, source) and 7.3 (an AI-inferred event must be
    # labelled as such and cite the signals it was inferred from — `signals` is that citation,
    # stored as JSON text per the design §1.1 array convention).
    """
    CREATE TABLE life_event (
      life_event_id   TEXT PRIMARY KEY,
      customer_id     TEXT NOT NULL REFERENCES customer(customer_id),
      life_event_type TEXT NOT NULL CHECK (life_event_type IN
                        ('MARRIAGE','DIVORCE','CHILD_BIRTH','HOME_PURCHASE','RELOCATION',
                         'JOB_CHANGE','PROMOTION','RETIREMENT','EDUCATION','BEREAVEMENT',
                         'BUSINESS_START','INHERITANCE')),
      event_date      TEXT NOT NULL CHECK (event_date IS date(event_date)),
      confidence      REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
      source          TEXT NOT NULL CHECK (source IN
                        ('SYSTEM_OF_RECORD','CUSTOMER_DECLARED','INFERRED')),
      is_inferred     INTEGER NOT NULL DEFAULT 0 CHECK (is_inferred IN (0,1)),
      signals         TEXT CHECK (signals IS NULL OR json_valid(signals)),
      as_of_date      TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK ((source = 'INFERRED') = (is_inferred = 1)),
      -- Requirement 7.2 displays a confidence score, and 7.3 requires cited signals, for an
      -- inferred event. Neither is renderable if the row omits them.
      CHECK (is_inferred = 0 OR (confidence IS NOT NULL AND signals IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_life_event_cust_date ON life_event(customer_id, event_date)",
    # ================================================================ campaigns and offers
    # Requirement 9.7: campaign membership and current status for the customer.
    """
    CREATE TABLE campaign (
      campaign_id     TEXT PRIMARY KEY,
      campaign_name   TEXT NOT NULL,
      business_group  TEXT NOT NULL CHECK (business_group IN
                        ('DEPOSITS','LENDING','CARDS','WEALTH','INSURANCE','BUSINESS')),
      channel         TEXT,
      start_date      TEXT NOT NULL CHECK (start_date IS date(start_date)),
      end_date        TEXT CHECK (end_date IS NULL OR end_date IS date(end_date)),
      campaign_status TEXT NOT NULL CHECK (campaign_status IN
                        ('PLANNED','ACTIVE','PAUSED','COMPLETED','CANCELLED')),
      as_of_date      TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (end_date IS NULL OR end_date >= start_date)
    )
    """,
    # Requirement 9.1: ID, name, business group, type, start date, end date, status.
    # `product_code` and `product_type` are what duplicate-product suppression (9.5) matches
    # against the customer's holdings, and `offer_type` is what distinguishes cross-sell from
    # upsell (9.4).
    """
    CREATE TABLE offer (
      offer_id       TEXT PRIMARY KEY,
      offer_name     TEXT NOT NULL,
      business_group TEXT NOT NULL CHECK (business_group IN
                       ('DEPOSITS','LENDING','CARDS','WEALTH','INSURANCE','BUSINESS')),
      offer_type     TEXT NOT NULL CHECK (offer_type IN
                       ('CROSS_SELL','UPSELL','RETENTION','ACQUISITION','SERVICE')),
      product_code   TEXT,
      product_type   TEXT,
      -- Expected value is `value_cents * acceptance_probability` (requirement 9.2); the
      -- per-customer probability lives on `customer_offer`, the offer's worth lives here.
      value_cents    INTEGER CHECK (value_cents IS NULL OR value_cents >= 0),
      start_date     TEXT NOT NULL CHECK (start_date IS date(start_date)),
      end_date       TEXT CHECK (end_date IS NULL OR end_date IS date(end_date)),
      offer_status   TEXT NOT NULL CHECK (offer_status IN
                       ('DRAFT','ACTIVE','EXPIRED','WITHDRAWN')),
      campaign_id    TEXT REFERENCES campaign(campaign_id),
      as_of_date     TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (end_date IS NULL OR end_date >= start_date)
    )
    """,
    "CREATE INDEX ix_offer_campaign ON offer(campaign_id)",
    # The customer's relationship to an offer. Requirements 9.1 (reaction, acceptance
    # probability), 9.3 (calibrated percentage with a confidence indicator), 9.5 (suppression with
    # a recorded reason) and 9.6 (a declined offer is not re-ranked into the top position within a
    # cooling-off window, which needs the reaction date).
    """
    CREATE TABLE customer_offer (
      customer_offer_id      TEXT PRIMARY KEY,
      customer_id            TEXT NOT NULL REFERENCES customer(customer_id),
      offer_id               TEXT NOT NULL REFERENCES offer(offer_id),
      event_date             TEXT NOT NULL CHECK (event_date IS date(event_date)),
      customer_reaction      TEXT NOT NULL DEFAULT 'PENDING' CHECK (customer_reaction IN
                               ('PENDING','VIEWED','INTERESTED','ACCEPTED','DECLINED','IGNORED')),
      reaction_date          TEXT CHECK (reaction_date IS NULL OR
                                         reaction_date IS date(reaction_date)),
      -- A probability, not a percentage: stored 0..1 and formatted at the API boundary, the same
      -- rule cents follow.
      acceptance_probability REAL CHECK (acceptance_probability IS NULL OR
                                         acceptance_probability BETWEEN 0 AND 1),
      probability_confidence TEXT CHECK (probability_confidence IS NULL OR
                               probability_confidence IN ('LOW','MEDIUM','HIGH')),
      expected_value_cents   INTEGER,
      affinity_basis         TEXT,
      is_suppressed          INTEGER NOT NULL DEFAULT 0 CHECK (is_suppressed IN (0,1)),
      suppression_reason     TEXT,
      channel                TEXT,
      as_of_date             TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      -- One row per customer per offer; a re-presentation updates the reaction rather than
      -- appending, which keeps "has this customer declined this offer" a single-row question.
      UNIQUE (customer_id, offer_id),
      CHECK (reaction_date IS NULL OR reaction_date >= event_date),
      CHECK ((customer_reaction = 'PENDING') = (reaction_date IS NULL)),
      -- Requirement 9.5 requires the suppression reason to be *visible* to entitled users, so a
      -- suppressed row without one cannot satisfy it.
      CHECK (is_suppressed = 0 OR suppression_reason IS NOT NULL),
      CHECK (acceptance_probability IS NULL OR probability_confidence IS NOT NULL)
    )
    """,
    "CREATE INDEX ix_customer_offer_cust_date ON customer_offer(customer_id, event_date)",
    "CREATE INDEX ix_customer_offer_offer ON customer_offer(offer_id)",
    # ================================================================ relationships
    # Requirement 6.1 (household members) and design §4.5's household-rollup index. The composite
    # primary key supplies the `(household_id, customer_id)` index §4.5 calls `ix_hhmember`;
    # creating a second identical index alongside SQLite's implicit primary-key index would cost
    # writes and buy nothing.
    """
    CREATE TABLE household_member (
      household_id TEXT NOT NULL REFERENCES household(household_id),
      customer_id  TEXT NOT NULL REFERENCES customer(customer_id),
      member_role  TEXT NOT NULL CHECK (member_role IN
                     ('HEAD','SPOUSE','PARTNER','CHILD','DEPENDENT','OTHER')),
      joined_date  TEXT CHECK (joined_date IS NULL OR joined_date IS date(joined_date)),
      as_of_date   TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      PRIMARY KEY (household_id, customer_id)
    )
    """,
    # A customer can only be looked up by household through the PK's leading column, so the
    # reverse direction needs its own index for "which household is this customer in".
    "CREATE INDEX ix_household_member_cust ON household_member(customer_id)",
    # Requirement 6.6: inferred relationships are labelled and carry a confidence score.
    """
    CREATE TABLE customer_relationship (
      relationship_id   TEXT PRIMARY KEY,
      from_customer_id  TEXT NOT NULL REFERENCES customer(customer_id),
      to_customer_id    TEXT NOT NULL REFERENCES customer(customer_id),
      relationship_type TEXT NOT NULL CHECK (relationship_type IN
                          ('SPOUSE','PARTNER','PARENT','CHILD','SIBLING','GUARDIAN',
                           'BUSINESS_PARTNER','REFERRED_BY','ADVISOR','OTHER')),
      is_inferred       INTEGER NOT NULL DEFAULT 0 CHECK (is_inferred IN (0,1)),
      confidence        REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
      -- What the inference was drawn from, e.g. a shared address hash. Requirement 6.6 needs the
      -- label and the score; this is what makes the label defensible when someone asks why.
      inference_basis   TEXT,
      source_system     TEXT NOT NULL,
      as_of_date        TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (from_customer_id <> to_customer_id),
      UNIQUE (from_customer_id, to_customer_id, relationship_type),
      CHECK (is_inferred = 0 OR (confidence IS NOT NULL AND inference_basis IS NOT NULL))
    )
    """,
    "CREATE INDEX ix_relationship_from ON customer_relationship(from_customer_id)",
    "CREATE INDEX ix_relationship_to ON customer_relationship(to_customer_id)",
    # Requirement 6.1: joint account holders. `ownership_bps` is basis points, so a 1/3 share is
    # 3333 and the shares on an account are checkable integers rather than drifting floats.
    """
    CREATE TABLE account_party (
      account_id    TEXT NOT NULL REFERENCES account(account_id),
      customer_id   TEXT NOT NULL REFERENCES customer(customer_id),
      party_role    TEXT NOT NULL CHECK (party_role IN
                      ('PRIMARY','JOINT','AUTHORIZED_USER','CUSTODIAN','POWER_OF_ATTORNEY')),
      ownership_bps INTEGER CHECK (ownership_bps IS NULL OR
                                   ownership_bps BETWEEN 0 AND 10000),
      added_date    TEXT CHECK (added_date IS NULL OR added_date IS date(added_date)),
      as_of_date    TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      PRIMARY KEY (account_id, customer_id, party_role)
    )
    """,
    "CREATE INDEX ix_account_party_cust ON account_party(customer_id)",
    # Requirement 6.1 (beneficiaries) with the §4.3 inferred/confidence pair.
    # `beneficiary_customer_id` is nullable because a named beneficiary is frequently not a
    # customer of the bank; `beneficiary_name` is always present so the relationship is displayable
    # either way.
    """
    CREATE TABLE beneficiary (
      beneficiary_id          TEXT PRIMARY KEY,
      account_id              TEXT NOT NULL REFERENCES account(account_id),
      beneficiary_customer_id TEXT REFERENCES customer(customer_id),
      beneficiary_name        TEXT NOT NULL,
      relationship            TEXT,
      share_bps               INTEGER NOT NULL CHECK (share_bps BETWEEN 0 AND 10000),
      is_inferred             INTEGER NOT NULL DEFAULT 0 CHECK (is_inferred IN (0,1)),
      confidence              REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
      as_of_date              TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (is_inferred = 0 OR confidence IS NOT NULL)
    )
    """,
    "CREATE INDEX ix_beneficiary_account ON beneficiary(account_id)",
    "CREATE INDEX ix_beneficiary_cust ON beneficiary(beneficiary_customer_id)",
)

_DROP_ORDER: Statements = (
    "beneficiary",
    "account_party",
    "customer_relationship",
    "household_member",
    "customer_offer",
    "offer",
    "campaign",
    "life_event",
    "customer_event",
    "application",
    "vehicle",
    "property",
    "asset",
)


def upgrade() -> None:
    execute_all(_UPGRADE)


def downgrade() -> None:
    drop_tables(_DROP_ORDER)
