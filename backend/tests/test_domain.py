"""Domain model, enum and port tests (task 1.6).

The load-bearing test in this module is
:func:`test_every_enum_matches_its_check_constraint`. Every closed value set in this schema is
written
down twice — once as ``CHECK (column IN (...))`` in a migration, once as a ``StrEnum`` in
:mod:`c360.domain.enums` — and two definitions of one thing drift. The drift is quiet, too: someone
adds ``'PARTNER'`` to a constraint and not to the enum, and the consequence is a ``ValidationError``
on
one customer in one cohort, months later, in whichever module happens to read that column first.

So the constraint is read back out of ``sqlite_master`` and compared to the enum, for every pair. It
is the only mechanism that makes keeping them in sync automatic rather than a review convention.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.migrations import upgrade_database
from c360.data.repositories import build_repositories
from c360.domain import enums
from c360.domain.enums import (
    AccountStatus,
    AccountType,
    ApplicationStatus,
    AssetType,
    BusinessGroup,
    CampaignStatus,
    CustomerReaction,
    CustomerSegment,
    CustomerType,
    CustomerValue,
    DelinquencyStatus,
    DepositProductType,
    EngagementChannel,
    EngagementEventType,
    EngagementOutcome,
    FraudResult,
    HouseholdRole,
    InvestmentRiskProfile,
    LifeEventSource,
    LifeEventType,
    LoanType,
    OfferStatus,
    OfferType,
    OwnershipType,
    PartyRole,
    ProbabilityConfidence,
    PropertyType,
    RelationshipType,
    RiskBand,
)
from c360.domain.models import (
    Account,
    Asset,
    AssetHolding,
    CategoryTotal,
    Customer,
    CustomerOffer,
    Deposit,
    DomainModel,
    Holding,
    Loan,
    MonthlyTotal,
    Offer,
    OfferForCustomer,
    SourcedModel,
)
from c360.domain.money import Cents
from c360.domain.ports import (
    CustomerRepository,
    FinancialRepository,
    JourneyRepository,
    OfferRepository,
    RelationshipRepository,
    RiskRepository,
)

#: Enums computed from a stored value rather than persisted in a column of their own, so they have
#: no ``CHECK`` constraint to match. ``RiskBand`` is derived from ``risk_profile.risk_score``.
_DERIVED_ENUMS: frozenset[type[StrEnum]] = frozenset({RiskBand})

#: ``(table, column) -> enum``. Every ``CHECK (column IN (...))`` in the migrations appears here.
ENUM_COLUMNS: dict[tuple[str, str], type[StrEnum]] = {
    ("customer", "customer_type"): CustomerType,
    ("customer", "customer_segment"): CustomerSegment,
    ("customer", "customer_value"): CustomerValue,
    ("account", "account_type"): AccountType,
    ("account", "account_status"): AccountStatus,
    ("deposit", "product_type"): DepositProductType,
    ("loan", "loan_type"): LoanType,
    ("investment", "investment_risk_profile"): InvestmentRiskProfile,
    ("risk_profile", "delinquency_status"): DelinquencyStatus,
    ("asset", "asset_type"): AssetType,
    ("asset", "ownership_type"): OwnershipType,
    ("property", "property_type"): PropertyType,
    ("application", "application_status"): ApplicationStatus,
    ("application", "fraud_result"): FraudResult,
    ("customer_event", "event_type"): EngagementEventType,
    ("customer_event", "channel"): EngagementChannel,
    ("customer_event", "outcome"): EngagementOutcome,
    ("life_event", "life_event_type"): LifeEventType,
    ("life_event", "source"): LifeEventSource,
    ("campaign", "business_group"): BusinessGroup,
    ("campaign", "campaign_status"): CampaignStatus,
    ("offer", "business_group"): BusinessGroup,
    ("offer", "offer_type"): OfferType,
    ("offer", "offer_status"): OfferStatus,
    ("customer_offer", "customer_reaction"): CustomerReaction,
    ("customer_offer", "probability_confidence"): ProbabilityConfidence,
    ("household_member", "member_role"): HouseholdRole,
    ("customer_relationship", "relationship_type"): RelationshipType,
    ("account_party", "party_role"): PartyRole,
}


@pytest.fixture(scope="module")
def schema_sql(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """``table -> CREATE TABLE`` text, read back from the migrated database."""
    path = tmp_path_factory.mktemp("domain") / "customer.db"
    upgrade_database(path)
    engine = create_sqlite_engine(path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL")
            ).all()
    finally:
        engine.dispose()
    return {str(row[0]): str(row[1]) for row in rows}


@pytest.fixture(scope="module")
def engine_for_ports(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    path = tmp_path_factory.mktemp("ports") / "customer.db"
    upgrade_database(path)
    engine = create_sqlite_engine(path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        yield engine
    finally:
        engine.dispose()


def _check_constraint_values(table_sql: str, column: str) -> set[str]:
    """Extract the literal set from the first ``<column> IN (...)`` in a ``CREATE TABLE``.

    The *first* match is the column-level constraint. A later one can exist — ``application`` has
    ``CHECK ((application_status IN ('APPROVED','DECLINED','FUNDED')) = (decision_date IS NOT
    NULL))``
    expressing a different rule — and taking the first keeps this reading the value set rather than
    a
    derived subset.
    """
    match = re.search(rf"\b{re.escape(column)}\s+IN\s*\(([^)]*)\)", table_sql)
    if match is None:
        raise AssertionError(f"no `{column} IN (...)` CHECK constraint found")
    return set(re.findall(r"'([^']*)'", match.group(1)))


# ================================================================ enum / schema parity
@pytest.mark.parametrize(
    ("table", "column"),
    list(ENUM_COLUMNS),
    ids=[f"{table}.{column}" for table, column in ENUM_COLUMNS],
)
def test_every_enum_matches_its_check_constraint(
    schema_sql: dict[str, str], table: str, column: str
) -> None:
    enum_type = ENUM_COLUMNS[(table, column)]
    in_schema = _check_constraint_values(schema_sql[table], column)
    in_code = {member.value for member in enum_type}
    assert in_schema == in_code, (
        f"{table}.{column} and {enum_type.__name__} disagree: "
        f"only in schema {in_schema - in_code}, only in code {in_code - in_schema}"
    )


def test_the_mapping_covers_every_enum_in_the_module(schema_sql: dict[str, str]) -> None:
    """A new enum with no schema counterpart is a value set nothing enforces.

    Without this, adding an enum and forgetting to add it to :data:`ENUM_COLUMNS` would leave the
    parity test silently not covering it.
    """
    del schema_sql
    defined = {
        value
        for value in vars(enums).values()
        if isinstance(value, type) and issubclass(value, StrEnum) and value is not StrEnum
    }
    # Derived enums are computed from a stored value, never persisted in a column of their own, so
    # they have no CHECK constraint to match. RiskBand is bucketed from risk_profile.risk_score by
    # the risk module; asserting it against a column would be asserting a constraint that (rightly)
    # does not exist.
    mapped = set(ENUM_COLUMNS.values()) | _DERIVED_ENUMS
    assert defined == mapped, f"unmapped enums: {sorted(e.__name__ for e in defined - mapped)}"


def test_enum_members_are_their_own_values() -> None:
    """``StrEnum`` members bind straight into SQL and JSON, with no ``.value`` at the boundary."""
    assert AccountType.DEPOSIT == "DEPOSIT"
    assert str(CustomerSegment.HNW) == "HNW"
    assert f"{OfferType.CROSS_SELL}" == "CROSS_SELL"


def test_application_status_decided_set_matches_the_schema(schema_sql: dict[str, str]) -> None:
    """:attr:`ApplicationStatus.is_decided` mirrors a second ``CHECK`` on the same column."""
    sql = schema_sql["application"]
    match = re.search(
        r"\(application_status IN \(([^)]*)\)\)\s*=\s*\n?\s*\(decision_date IS NOT NULL\)", sql
    )
    assert match is not None, "the decided-status CHECK is no longer in the shape this test reads"
    in_schema = set(re.findall(r"'([^']*)'", match.group(1)))
    in_code = {status.value for status in ApplicationStatus if status.is_decided}
    assert in_schema == in_code


# ================================================================ model behaviour
def _customer_row() -> dict[str, object]:
    return {
        "customer_id": "C-1",
        "customer_name": "Test Person",
        "customer_type": "INDIVIDUAL",
        "customer_segment": "MASS",
        "customer_since": "2020-01-01",
        "customer_value": "SILVER",
        "as_of_date": "2026-09-01",
        "source_system": "CORE",
    }


def test_models_validate_iso_text_into_dates() -> None:
    customer = Customer.model_validate(_customer_row())
    assert customer.customer_since == date(2020, 1, 1)
    assert customer.as_of_date == date(2026, 9, 1)
    assert customer.date_of_birth is None


@pytest.mark.parametrize("bad", ["2020-1-1", "20200101", "2020-13-01", "not-a-date", ""])
def test_models_reject_dates_the_database_would_reject(bad: str) -> None:
    """The model layer is exactly as strict as the ``CHECK`` constraint, not looser."""
    from pydantic import ValidationError  # noqa: PLC0415

    row = _customer_row() | {"customer_since": bad}
    with pytest.raises(ValidationError):
        Customer.model_validate(row)


def test_models_reject_a_datetime_in_a_date_column() -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    row = _customer_row() | {"customer_since": datetime(2020, 1, 1, tzinfo=UTC)}
    with pytest.raises(ValidationError, match="not datetimes"):
        Customer.model_validate(row)


def test_models_are_frozen() -> None:
    """A read model a service can mutate is one that can be mutated after masking."""
    from pydantic import ValidationError  # noqa: PLC0415

    customer = Customer.model_validate(_customer_row())
    with pytest.raises(ValidationError):
        customer.customer_name = "Someone Else"  # type: ignore[misc]


def test_models_reject_unknown_columns() -> None:
    """A migration adding a column must fail loudly here, not silently stop being displayed."""
    from pydantic import ValidationError  # noqa: PLC0415

    row = _customer_row() | {"newly_added_column": "surprise"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Customer.model_validate(row)


def test_every_model_carries_provenance() -> None:
    """Task 1.6's requirement, asserted structurally rather than one model at a time."""
    # `CustomerSearchHit` is an FTS projection for a picker, not an observation of a customer, so it
    # carries no provenance for the same reason `CategoryTotal` and `MonthlyTotal` do not.
    composites = {
        "Holding",
        "AssetHolding",
        "OfferForCustomer",
        "CategoryTotal",
        "MonthlyTotal",
        "CustomerSearchHit",
        # A cohort row is a book-level picker projection (id, name, coarse band/segment/value), not
        # an observation of a customer, so it carries no provenance for the same reason
        # CustomerSearchHit does not.
        "CustomerCohortHit",
    }
    from c360.domain import models as models_module  # noqa: PLC0415

    checked = 0
    for name, value in vars(models_module).items():
        if not (isinstance(value, type) and issubclass(value, DomainModel)):
            continue
        if name in {"DomainModel", "SourcedModel"} or name in composites:
            continue
        assert issubclass(value, SourcedModel), f"{name} does not carry provenance"
        assert {"as_of_date", "source_system"} <= set(value.model_fields)
        checked += 1
    assert checked >= 20, "the sweep stopped finding models; has the module been renamed?"


def test_composite_models_delegate_provenance() -> None:
    """``Holding`` has no columns of its own, so its as-of comes from the account."""
    account = Account.model_validate(
        {
            "account_id": "A-1",
            "customer_id": "C-1",
            "account_number": "1",
            "account_type": "DEPOSIT",
            "balance_cents": 1000,
            "account_status": "ACTIVE",
            "open_date": "2020-01-01",
            "as_of_date": "2026-09-01",
            "source_system": "CORE",
        }
    )
    holding = Holding(account=account)
    assert holding.as_of_date == date(2026, 9, 1)
    assert holding.source_system == "CORE"
    assert holding.detail is None


def test_product_detail_union_is_discriminated() -> None:
    """``kind`` decides the member, so validation does not depend on smart-union heuristics."""
    base = {"as_of_date": "2026-09-01", "source_system": "CORE", "account_id": "A-1"}
    deposit = Holding.model_validate(
        {
            "account": {
                "account_id": "A-1",
                "customer_id": "C-1",
                "account_number": "1",
                "account_type": "DEPOSIT",
                "balance_cents": 1000,
                "account_status": "ACTIVE",
                "open_date": "2020-01-01",
                **base,
            },
            "detail": {"kind": "DEPOSIT", "product_type": "SAVINGS", **base},
        }
    )
    assert isinstance(deposit.detail, Deposit)
    assert deposit.detail.product_type is DepositProductType.SAVINGS

    loan = Loan.model_validate(
        {"kind": "LOAN", "loan_number": "L-1", "original_amount_cents": 500, **base}
    )
    assert loan.kind is AccountType.LOAN
    assert isinstance(loan.original_amount_cents, Cents)


def test_aggregate_models_constrain_their_shape() -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    assert CategoryTotal.model_validate(
        {"transaction_category": "GROCERIES", "total_cents": 100, "transaction_count": 2}
    ).total_cents == Cents(100)

    assert (
        MonthlyTotal.model_validate(
            {"month": "2026-08", "total_cents": 100, "transaction_count": 1}
        ).month
        == "2026-08"
    )

    # A month identifier is not a date, and giving it a day would invite treating it as one.
    with pytest.raises(ValidationError):
        MonthlyTotal.model_validate(
            {"month": "2026-08-01", "total_cents": 100, "transaction_count": 1}
        )
    with pytest.raises(ValidationError):
        CategoryTotal.model_validate(
            {"transaction_category": "X", "total_cents": 1, "transaction_count": -1}
        )


# ================================================================ ports
def test_every_adapter_satisfies_its_port(engine_for_ports: Engine) -> None:
    """Structural conformance at runtime. ``mypy`` checks the signatures; this checks the wiring."""
    repos = build_repositories(engine_for_ports)
    assert isinstance(repos.customer, CustomerRepository)
    assert isinstance(repos.financial, FinancialRepository)
    assert isinstance(repos.relationship, RelationshipRepository)
    assert isinstance(repos.risk, RiskRepository)
    assert isinstance(repos.offer, OfferRepository)
    assert isinstance(repos.journey, JourneyRepository)


def test_ports_declare_no_write_methods() -> None:
    """Requirement A1 makes the platform read-only over source data; the ports say so in shape."""
    forbidden = ("save", "insert", "update", "delete", "upsert", "create", "write")
    for port in (
        CustomerRepository,
        FinancialRepository,
        RelationshipRepository,
        RiskRepository,
        OfferRepository,
        JourneyRepository,
    ):
        methods = [name for name in vars(port) if not name.startswith("_")]
        for method in methods:
            assert not method.startswith(forbidden), f"{port.__name__}.{method} looks like a write"


def test_a_hand_written_double_satisfies_the_port() -> None:
    """``Protocol`` over ABC: a test double needs the right methods, not an inheritance chain."""

    class StubCustomerRepository:
        def get(self, customer_id: str) -> Customer | None:
            return Customer.model_validate(_customer_row()) if customer_id == "C-1" else None

        def exists(self, customer_id: str) -> bool:
            return customer_id == "C-1"

        def get_contact_info(self, customer_id: str) -> None:
            return None

        def get_employer_for_customer(self, customer_id: str) -> None:
            return None

        def get_household(self, household_id: str) -> None:
            return None

        def list_ids(self, *, limit: int, after: str | None = None) -> tuple[str, ...]:
            return ("C-1",)

        def count(self) -> int:
            return 1

    stub = StubCustomerRepository()
    assert isinstance(stub, CustomerRepository)
    port: CustomerRepository = stub
    assert port.exists("C-1")
    assert port.get("C-2") is None


# ================================================================ layering
def test_domain_package_does_not_depend_on_storage() -> None:
    """Design §2.2: services depend on ports, adapters on both, and the domain on neither.

    This is what makes the PostgreSQL swap path in design §1 a new adapter rather than a rewrite,
    and
    it is the kind of rule that erodes one convenient import at a time.
    """
    domain_dir = Path(__file__).resolve().parents[1] / "src" / "c360" / "domain"
    forbidden = ("sqlalchemy", "sqlite3", "alembic", "c360.data", "fastapi")

    offenders: list[str] = []
    for module in sorted(domain_dir.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        # Only import statements, so prose in a docstring naming SQLAlchemy is not a violation.
        imports = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        offenders += [
            f"{module.name}: {line}" for line in imports if any(name in line for name in forbidden)
        ]
    assert not offenders, f"the domain layer reached into storage: {offenders}"


def test_asset_holding_and_offer_delegate_provenance() -> None:
    """The composite models have no columns, so their as-of comes from the row that does."""
    asset = Asset.model_validate(
        {
            "asset_id": "AS-1",
            "customer_id": "C-1",
            "asset_type": "VEHICLE",
            "current_value_cents": 100,
            "is_collateral": 0,
            "as_of_date": "2026-09-01",
            "source_system": "COLLATERAL_SYS",
        }
    )
    holding = AssetHolding(asset=asset)
    assert holding.as_of_date == date(2026, 9, 1)
    assert holding.source_system == "COLLATERAL_SYS"
    assert holding.detail is None

    offer = Offer.model_validate(
        {
            "offer_id": "O-1",
            "offer_name": "Test Offer",
            "business_group": "WEALTH",
            "offer_type": "UPSELL",
            "start_date": "2026-01-01",
            "offer_status": "ACTIVE",
            "as_of_date": "2026-08-01",
            "source_system": "CAMPAIGN",
        }
    )
    customer_offer = CustomerOffer.model_validate(
        {
            "customer_offer_id": "CO-1",
            "customer_id": "C-1",
            "offer_id": "O-1",
            "event_date": "2026-08-05",
            "customer_reaction": "PENDING",
            "is_suppressed": 0,
            "as_of_date": "2026-09-01",
            "source_system": "CRM",
        }
    )
    joined = OfferForCustomer(offer=offer, customer_offer=customer_offer)
    # The customer's row, not the catalogue's: the as-of that matters is when this customer's
    # relationship to the offer was observed.
    assert joined.as_of_date == date(2026, 9, 1)
    assert joined.source_system == "CRM"
