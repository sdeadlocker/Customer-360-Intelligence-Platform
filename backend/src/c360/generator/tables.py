"""Table definitions and load order for the generator (tasks 2.1, 2.8).

The generators build rows as positional tuples and the loader inserts them with ``executemany``
(task 2.8), which means the column order in an ``INSERT`` and the value order in a row have to
agree.
Declaring each table once, here, is what keeps that agreement checkable: :meth:`Dataset.add`
validates the arity of every row against the declaration, so a column inserted in the middle of a
list fails on the first row rather than producing a database in which two columns are swapped and
every value is individually plausible.

Load order is the other thing this module fixes, and it is not the order design §15 lists.
-----------------------------------------------------------------------------------------

Design §15 gives the generation order as "... accounts → specializations → transactions → assets →
property/vehicle → relationships ...". Loading in that order fails: ``loan.collateral_asset_id``
references ``asset`` (migration ``0002``), so with ``foreign_keys = ON`` — which
:mod:`c360.data.engine` sets on every connection, writers included — inserting a collateralized loan
before its collateral raises ``FOREIGN KEY constraint failed``.

:data:`LOAD_ORDER` therefore moves ``asset``, ``property`` and ``vehicle`` ahead of ``loan``. It is
a
load-order change only; nothing about which values are generated moves with it, because the
generators run over a plan that is complete before any row is emitted (see
:mod:`c360.generator.plan`).

The profile tables come last because they aggregate what the product and asset generators produced.
``financial_profile`` carries ``CHECK (net_worth_cents = total_assets_cents -
total_liabilities_cents)``, so it cannot be written from an estimate — it has to be summed from the
rows that were actually emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: A value bindable as a SQLite parameter. The four storage classes SQLite has, plus NULL.
#:
#: ``Cents`` and ``Bps`` are ``int`` subclasses (see :mod:`c360.domain.money`) so they satisfy this
#: without a conversion step, which is the whole reason they subclass ``int``.
RowValue = str | int | float | None

#: One row, positional, in the column order of its :class:`Table`.
Row = tuple[RowValue, ...]


@dataclass(frozen=True, slots=True)
class Table:
    """A target table and its column order."""

    name: str
    columns: tuple[str, ...]

    @property
    def insert_sql(self) -> str:
        """A parameterized ``INSERT`` for :meth:`sqlite3.Cursor.executemany`.

        Qmark placeholders rather than named ones because the rows are positional tuples, and
        building a dict per row for ~20,000 transactions is pure overhead on the one table where
        load time is measurable.

        The statement is assembled from this module's own constants — table and column names that
        are literals in the source below — so there is no path for external input to reach it.
        """
        placeholders = ", ".join("?" * len(self.columns))
        return (
            f"INSERT INTO {self.name} ({', '.join(self.columns)}) "  # noqa: S608 - see docstring
            f"VALUES ({placeholders})"
        )


# ---------------------------------------------------------------- reference and identity
EMPLOYER = Table(
    "employer",
    ("employer_id", "employer_name", "industry", "city", "state"),
)

HOUSEHOLD = Table(
    "household",
    (
        "household_id",
        "household_name",
        "primary_customer_id",
        "address_hash",
        "member_count",
        "as_of_date",
    ),
)

CUSTOMER = Table(
    "customer",
    (
        "customer_id",
        "customer_name",
        "customer_type",
        "customer_segment",
        "customer_since",
        "date_of_birth",
        "citizenship",
        "occupation",
        "employer_id",
        "employment_status",
        "marital_status",
        "customer_value",
        "customer_value_score",
        "preferred_language",
        "preferred_channel",
        "household_id",
        "as_of_date",
        "source_system",
    ),
)

CONTACT_INFO = Table(
    "contact_info",
    (
        "customer_id",
        "email",
        "phone_number",
        "mobile_number",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "country",
        "postal_code",
        "as_of_date",
    ),
)

# ---------------------------------------------------------------- products
ACCOUNT = Table(
    "account",
    (
        "account_id",
        "customer_id",
        "account_number",
        "account_type",
        "product_name",
        "product_code",
        "balance_cents",
        "available_balance_cents",
        "interest_rate_bps",
        "account_status",
        "open_date",
        "close_date",
        "as_of_date",
    ),
)

DEPOSIT = Table(
    "deposit",
    ("account_id", "product_type", "household_deposits_cents", "maturity_date"),
)

LOAN = Table(
    "loan",
    (
        "account_id",
        "loan_number",
        "loan_type",
        "original_amount_cents",
        "monthly_emi_cents",
        "loan_status",
        "loan_start_date",
        "loan_end_date",
        "collateral_asset_id",
    ),
)

CREDIT_CARD = Table(
    "credit_card",
    (
        "account_id",
        "card_number",
        "card_last4",
        "card_type",
        "credit_limit_cents",
        "utilization_bps",
        "rewards_balance_cents",
        "monthly_spend_cents",
        "overlimit_events",
        "fraud_alerts",
    ),
)

INVESTMENT = Table(
    "investment",
    (
        "account_id",
        "portfolio_value_cents",
        "asset_allocation",
        "mutual_funds_cents",
        "stocks_cents",
        "bonds_cents",
        "retirement_accounts_cents",
        "investment_risk_profile",
    ),
)

TXN = Table(
    "txn",
    (
        "transaction_id",
        "account_id",
        "customer_id",
        "transaction_date",
        "amount_cents",
        "transaction_type",
        "transaction_category",
        "merchant",
        "channel",
        "status",
    ),
)

# ---------------------------------------------------------------- assets
ASSET = Table(
    "asset",
    (
        "asset_id",
        "customer_id",
        "asset_type",
        "asset_description",
        "current_value_cents",
        "ownership_type",
        "acquired_date",
        "is_collateral",
        "as_of_date",
        "source_system",
    ),
)

PROPERTY = Table(
    "property",
    (
        "asset_id",
        "property_type",
        "address_line1",
        "city",
        "state",
        "postal_code",
        "purchase_price_cents",
        "assessed_value_cents",
        "square_feet",
        "year_built",
    ),
)

VEHICLE = Table(
    "vehicle",
    ("asset_id", "make", "model", "model_year", "vin", "mileage", "purchase_price_cents"),
)

# ---------------------------------------------------------------- relationships
HOUSEHOLD_MEMBER = Table(
    "household_member",
    ("household_id", "customer_id", "member_role", "joined_date", "as_of_date"),
)

CUSTOMER_RELATIONSHIP = Table(
    "customer_relationship",
    (
        "relationship_id",
        "from_customer_id",
        "to_customer_id",
        "relationship_type",
        "is_inferred",
        "confidence",
        "inference_basis",
        "source_system",
        "as_of_date",
    ),
)

ACCOUNT_PARTY = Table(
    "account_party",
    ("account_id", "customer_id", "party_role", "ownership_bps", "added_date", "as_of_date"),
)

BENEFICIARY = Table(
    "beneficiary",
    (
        "beneficiary_id",
        "account_id",
        "beneficiary_customer_id",
        "beneficiary_name",
        "relationship",
        "share_bps",
        "is_inferred",
        "confidence",
        "as_of_date",
    ),
)

# ---------------------------------------------------------------- journey
APPLICATION = Table(
    "application",
    (
        "application_id",
        "customer_id",
        "product_applied",
        "product_code",
        "event_date",
        "channel",
        "application_status",
        "fraud_result",
        "decision_date",
        "requested_amount_cents",
        "approved_amount_cents",
        "account_id",
        "as_of_date",
        "source_system",
    ),
)

CAMPAIGN = Table(
    "campaign",
    (
        "campaign_id",
        "campaign_name",
        "business_group",
        "channel",
        "start_date",
        "end_date",
        "campaign_status",
        "as_of_date",
    ),
)

OFFER = Table(
    "offer",
    (
        "offer_id",
        "offer_name",
        "business_group",
        "offer_type",
        "product_code",
        "product_type",
        "value_cents",
        "start_date",
        "end_date",
        "offer_status",
        "campaign_id",
        "as_of_date",
    ),
)

CUSTOMER_OFFER = Table(
    "customer_offer",
    (
        "customer_offer_id",
        "customer_id",
        "offer_id",
        "event_date",
        "customer_reaction",
        "reaction_date",
        "acceptance_probability",
        "probability_confidence",
        "expected_value_cents",
        "affinity_basis",
        "is_suppressed",
        "suppression_reason",
        "channel",
        "as_of_date",
    ),
)

CUSTOMER_EVENT = Table(
    "customer_event",
    (
        "event_id",
        "customer_id",
        "event_type",
        "event_date",
        "channel",
        "session_id",
        "device_type",
        "outcome",
        "notes",
        "as_of_date",
    ),
)

LIFE_EVENT = Table(
    "life_event",
    (
        "life_event_id",
        "customer_id",
        "life_event_type",
        "event_date",
        "confidence",
        "source",
        "is_inferred",
        "signals",
        "as_of_date",
    ),
)

# ---------------------------------------------------------------- derived profiles
FINANCIAL_PROFILE = Table(
    "financial_profile",
    (
        "customer_id",
        "total_deposits_cents",
        "total_loans_cents",
        "total_investments_cents",
        "total_assets_cents",
        "total_liabilities_cents",
        "net_worth_cents",
        "household_net_worth_cents",
        "monthly_income_cents",
        "monthly_expense_cents",
        "as_of_date",
    ),
)

CREDIT_PROFILE = Table(
    "credit_profile",
    (
        "customer_id",
        "fico_score",
        "behavior_score",
        "propensity_score",
        "credit_utilization_bps",
        "years_on_bureau",
        "num_inquiries",
        "num_trades",
        "num_credit_accounts",
        "credit_exposure_cents",
        "as_of_date",
    ),
)

RISK_PROFILE = Table(
    "risk_profile",
    (
        "customer_id",
        "risk_score",
        "fraud_score",
        "pid_score",
        "sid_score",
        "delinquency_status",
        "current_days_past_due",
        "default_indicator",
        "chargeoff_indicator",
        "aml_flag",
        "pep_flag",
        "as_of_date",
    ),
)


#: Insertion order. Parents before children; see the module docstring on why ``asset`` precedes
#: ``loan`` rather than following ``txn`` as design §15's prose has it.
LOAD_ORDER: Final[tuple[Table, ...]] = (
    EMPLOYER,
    HOUSEHOLD,
    CUSTOMER,
    CONTACT_INFO,
    ACCOUNT,
    DEPOSIT,
    ASSET,
    PROPERTY,
    VEHICLE,
    LOAN,
    CREDIT_CARD,
    INVESTMENT,
    TXN,
    HOUSEHOLD_MEMBER,
    CUSTOMER_RELATIONSHIP,
    ACCOUNT_PARTY,
    BENEFICIARY,
    APPLICATION,
    CAMPAIGN,
    OFFER,
    CUSTOMER_OFFER,
    CUSTOMER_EVENT,
    LIFE_EVENT,
    FINANCIAL_PROFILE,
    CREDIT_PROFILE,
    RISK_PROFILE,
)


class RowArityError(ValueError):
    """Raised when a row's length does not match its table's column count.

    Its own type because it is a programming error in a generator, not invalid data: the row cannot
    be repaired, and the ``INSERT`` it would produce would either fail or silently bind values to
    the wrong columns.
    """


@dataclass(slots=True)
class Dataset:
    """Rows accumulated per table, ready for the loader.

    Insertion-ordered by :data:`LOAD_ORDER` rather than by when a generator first contributed, so
    the loader does not depend on which generator ran first.
    """

    rows: dict[str, list[Row]] = field(
        default_factory=lambda: {table.name: [] for table in LOAD_ORDER}
    )

    def add(self, table: Table, row: Row) -> None:
        """Append one row, checking its arity against ``table``."""
        if len(row) != len(table.columns):
            raise RowArityError(
                f"{table.name} takes {len(table.columns)} values, got {len(row)}: "
                f"columns are {table.columns}"
            )
        self.rows[table.name].append(row)

    def extend(self, table: Table, rows: list[Row]) -> None:
        """Append many rows, checking each one's arity."""
        for row in rows:
            self.add(table, row)

    def count(self, table: Table) -> int:
        """Number of rows staged for ``table``."""
        return len(self.rows[table.name])

    def counts(self) -> dict[str, int]:
        """Row counts per table, in load order. For the CLI summary and the gate assertions."""
        return {table.name: len(self.rows[table.name]) for table in LOAD_ORDER}

    def total_rows(self) -> int:
        return sum(len(rows) for rows in self.rows.values())
