"""Schema migration tests (tasks 1.2, 1.3, 1.4).

The Phase 1 gate asks that "migrations apply to an empty file" and that "FK enforcement [is] proven
by
tests". Asserting that ``upgrade`` returns without raising proves neither: a migration that created
no
tables would pass, and so would one whose foreign keys are never enforced.

So the assertions here are about the schema that results. Every table and index the design names is
present; every ``CHECK`` constraint actually rejects the value it exists to reject; the foreign keys
actually fail an orphan insert; and the whole set reverses cleanly, which is what makes a bad
migration
recoverable rather than a restore-from-backup.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from c360.data import migrations as migrations_module
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.migrations import (
    ALEMBIC_INI,
    MIGRATIONS_DIR,
    MigrationError,
    current_revision,
    downgrade_database,
    head_revision,
    is_up_to_date,
    upgrade_database,
)

# ---------------------------------------------------------------- expectations
# Design §4.3 names these tables. Held as a literal set rather than derived from the migrations, so
# that dropping a table from a migration is a test failure rather than a silently smaller schema.
EXPECTED_TABLES = frozenset(
    {
        # 0001 — core customer
        "employer",
        "household",
        "customer",
        "contact_info",
        "financial_profile",
        "credit_profile",
        "risk_profile",
        # 0002 — products and transactions
        "account",
        "deposit",
        "loan",
        "credit_card",
        "investment",
        "txn",
        # 0003 — events, offers, assets, relationships
        "asset",
        "property",
        "vehicle",
        "application",
        "customer_event",
        "life_event",
        "campaign",
        "offer",
        "customer_offer",
        "household_member",
        "customer_relationship",
        "account_party",
        "beneficiary",
        # 0004 — derived values, search index, graph projection (Phase 3)
        "derived_financial",
        "derived_credit",
        "derived_health",
        "customer_search",
        "graph_node",
        "graph_edge",
        "graph_adjacency",
        "graph_household_subgraph",
    }
)

#: FTS5 backs `customer_search` with these shadow tables. They are an implementation detail of the
#: virtual table, not part of the designed schema, so they are excluded from the "nothing beyond the
#: design" assertion rather than listed as designed tables.
_FTS5_SHADOW_TABLES = frozenset(
    {
        "customer_search_config",
        "customer_search_content",
        "customer_search_data",
        "customer_search_docsize",
        "customer_search_idx",
    }
)

#: Indexes design §4.3 and §4.5 name explicitly.
EXPECTED_INDEXES = frozenset(
    {
        "ix_txn_cust_date",
        "ix_txn_cust_cat",
        "ix_txn_acct_date",
        "ix_account_cust_type",
        "ix_account_number",
        "ix_loan_number",
        "ix_card_last4",
        "ix_customer_household",
        "ix_application_cust_date",
        "ix_customer_event_cust_date",
        "ix_life_event_cust_date",
        "ix_customer_offer_cust_date",
    }
)


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    """A database file migrated to head. The "applies to an empty file" half of the gate."""
    path = tmp_path / "customer.db"
    assert not path.exists()
    upgrade_database(path)
    return path


@pytest.fixture
def writer(migrated_db: Path) -> Iterator[Engine]:
    """A read-write engine on the migrated database, so constraint violations can be provoked."""
    engine = create_sqlite_engine(migrated_db, mode=AccessMode.READ_WRITE, pool_size=2)
    try:
        yield engine
    finally:
        engine.dispose()


def _names(engine: Engine, kind: str) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT name FROM sqlite_master " "WHERE type = :kind AND name NOT LIKE 'sqlite_%'"
            ),
            {"kind": kind},
        ).all()
    return {str(row[0]) for row in rows}


# ---------------------------------------------------------------- environment
def test_alembic_environment_is_present() -> None:
    assert ALEMBIC_INI.is_file()
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "script.py.mako").is_file()
    assert list((MIGRATIONS_DIR / "versions").glob("0*.py"))


def test_env_configures_batch_mode() -> None:
    """SQLite cannot ALTER a constraint, so batch mode has to be on before it is first needed.

    Asserted against the source because there is no migration in Phase 1 that exercises it — every
    one only creates tables. Waiting for the first ALTER to find out would mean discovering it in
    the
    migration that needs it, against a database that has already been built.
    """
    source = (MIGRATIONS_DIR / "env.py").read_text(encoding="utf-8")
    assert source.count("render_as_batch=True") == 2, "online and offline paths both need it"


def test_head_is_the_last_declared_revision() -> None:
    assert head_revision() == "0004_derived_search_graph"


# ---------------------------------------------------------------- application
def test_migrations_apply_to_an_empty_file(migrated_db: Path) -> None:
    assert migrated_db.is_file()
    assert current_revision(migrated_db) == head_revision()
    assert is_up_to_date(migrated_db)


def test_every_designed_table_exists(writer: Engine) -> None:
    tables = _names(writer, "table")
    assert tables >= EXPECTED_TABLES
    # Nothing beyond the design, Alembic's bookkeeping and FTS5's own shadow tables.
    assert tables - EXPECTED_TABLES - _FTS5_SHADOW_TABLES == {"alembic_version"}


def test_every_designed_index_exists(writer: Engine) -> None:
    assert _names(writer, "index") >= EXPECTED_INDEXES


def test_unmigrated_and_absent_files_report_no_revision(tmp_path: Path) -> None:
    assert current_revision(tmp_path / "absent.db") is None
    assert not is_up_to_date(tmp_path / "absent.db")

    empty = tmp_path / "empty.db"
    engine = create_sqlite_engine(empty, mode=AccessMode.READ_WRITE_CREATE, pool_size=1)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE unrelated (id TEXT)"))
    finally:
        engine.dispose()
    assert current_revision(empty) is None


def test_revision_probe_does_not_write(migrated_db: Path) -> None:
    """A readiness probe runs this repeatedly; it must not take a write lock or touch the file."""
    before = migrated_db.stat().st_mtime_ns
    for _ in range(3):
        assert current_revision(migrated_db) == head_revision()
    assert migrated_db.stat().st_mtime_ns == before


def test_downgrade_removes_the_whole_schema(migrated_db: Path) -> None:
    downgrade_database(migrated_db)
    engine = create_sqlite_engine(migrated_db, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        remaining = _names(engine, "table")
    finally:
        engine.dispose()
    assert remaining == {"alembic_version"}
    assert current_revision(migrated_db) is None


def test_upgrade_is_replayable_after_downgrade(migrated_db: Path) -> None:
    downgrade_database(migrated_db)
    upgrade_database(migrated_db)
    engine = create_sqlite_engine(migrated_db, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        assert _names(engine, "table") >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_stepwise_upgrade_reaches_the_same_schema(tmp_path: Path) -> None:
    """Applying revisions one at a time must land where applying them together lands.

    A migration that depends on state an earlier one happened to leave behind passes a single
    `upgrade head` and fails for anyone upgrading an existing deployment.
    """
    path = tmp_path / "stepwise.db"
    for revision in (
        "0001_core_customer_tables",
        "0002_products_and_transactions",
        "0003_events_offers_assets_relationships",
        "0004_derived_search_graph",
    ):
        upgrade_database(path, revision)
        assert current_revision(path) == revision

    engine = create_sqlite_engine(path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        assert _names(engine, "table") >= EXPECTED_TABLES
    finally:
        engine.dispose()


# ---------------------------------------------------------------- referential integrity
def test_foreign_key_check_is_clean_at_head(writer: Engine) -> None:
    """`loan.collateral_asset_id` references `asset`, created a migration later. Prove it resolved.

    At revision 0002 this check *errors* with `no such table: main.asset`, which is the documented
    and
    accepted intermediate state. At head it must come back empty.
    """
    with writer.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []


def test_orphan_insert_is_rejected(writer: Engine) -> None:
    with writer.begin() as connection, pytest.raises(IntegrityError, match="FOREIGN KEY"):
        connection.execute(
            text(
                "INSERT INTO contact_info (customer_id, as_of_date) "
                "VALUES ('C-nonexistent', '2026-01-01')"
            )
        )


def test_collateral_reference_resolves_to_asset(writer: Engine) -> None:
    """The forward reference declared in 0002 must be a live foreign key once 0003 has applied."""
    with writer.begin() as connection:
        _seed_customer(connection)
        connection.execute(
            text(
                "INSERT INTO account (account_id, customer_id, account_number, account_type, "
                "balance_cents, account_status, open_date, as_of_date) "
                "VALUES ('A1', 'C1', '0001', 'LOAN', 25000000, 'ACTIVE', "
                "'2020-01-01', '2026-01-01')"
            )
        )

    with writer.begin() as connection, pytest.raises(IntegrityError, match="FOREIGN KEY"):
        connection.execute(
            text(
                "INSERT INTO loan (account_id, loan_number, loan_type, original_amount_cents, "
                "collateral_asset_id) VALUES ('A1', 'L1', 'MORTGAGE', 30000000, 'ASSET-missing')"
            )
        )


# ---------------------------------------------------------------- check constraints
def _seed_customer(connection: Connection) -> None:
    """Insert the minimum viable customer. Used by the constraint tests as a foreign-key parent."""
    connection.execute(
        text(
            "INSERT INTO customer (customer_id, customer_name, customer_type, customer_segment, "
            "customer_since, customer_value, as_of_date, source_system) "
            "VALUES ('C1', 'Test Customer', 'INDIVIDUAL', 'MASS', '2020-01-01', 'SILVER', "
            "'2026-01-01', 'CORE')"
        )
    )


#: ``(description, SQL)`` pairs that each violate exactly one constraint from design §4.3.
REJECTED_INSERTS: tuple[tuple[str, str], ...] = (
    (
        "customer_type outside the enumerated set",
        "INSERT INTO customer (customer_id, customer_name, customer_type, customer_segment, "
        "customer_since, customer_value, as_of_date, source_system) "
        "VALUES ('C9', 'X', 'PARTNERSHIP', 'MASS', '2020-01-01', 'SILVER', '2026-01-01', 'CORE')",
    ),
    (
        "customer_since not a zero-padded ISO date",
        "INSERT INTO customer (customer_id, customer_name, customer_type, customer_segment, "
        "customer_since, customer_value, as_of_date, source_system) "
        "VALUES ('C9', 'X', 'INDIVIDUAL', 'MASS', '2020-1-1', 'SILVER', '2026-01-01', 'CORE')",
    ),
    (
        "customer_since carrying a time component",
        "INSERT INTO customer (customer_id, customer_name, customer_type, customer_segment, "
        "customer_since, customer_value, as_of_date, source_system) "
        "VALUES ('C9', 'X', 'INDIVIDUAL', 'MASS', '2020-01-01T00:00:00', 'SILVER', "
        "'2026-01-01', 'CORE')",
    ),
    (
        "as_of_date not a real calendar date",
        "INSERT INTO customer (customer_id, customer_name, customer_type, customer_segment, "
        "customer_since, customer_value, as_of_date, source_system) "
        "VALUES ('C9', 'X', 'INDIVIDUAL', 'MASS', '2020-01-01', 'SILVER', '2026-02-30', 'CORE')",
    ),
    (
        "fico_score below the bureau range",
        "INSERT INTO credit_profile (customer_id, fico_score, as_of_date) "
        "VALUES ('C1', 299, '2026-01-01')",
    ),
    (
        "fico_score above the bureau range",
        "INSERT INTO credit_profile (customer_id, fico_score, as_of_date) "
        "VALUES ('C1', 851, '2026-01-01')",
    ),
    (
        "net worth not equal to assets minus liabilities",
        "INSERT INTO financial_profile (customer_id, total_assets_cents, "
        "total_liabilities_cents, net_worth_cents, as_of_date) "
        "VALUES ('C1', 1000, 400, 700, '2026-01-01')",
    ),
    (
        "delinquency status contradicting days past due",
        "INSERT INTO risk_profile (customer_id, delinquency_status, current_days_past_due, "
        "as_of_date) VALUES ('C1', 'CURRENT', 45, '2026-01-01')",
    ),
    (
        "aml_flag outside 0/1",
        "INSERT INTO risk_profile (customer_id, aml_flag, as_of_date) "
        "VALUES ('C1', 2, '2026-01-01')",
    ),
    (
        "closed account with no close date",
        "INSERT INTO account (account_id, customer_id, account_number, account_type, "
        "balance_cents, account_status, open_date, as_of_date) "
        "VALUES ('A9', 'C1', '9', 'DEPOSIT', 0, 'CLOSED', '2020-01-01', '2026-01-01')",
    ),
    (
        "close date before open date",
        "INSERT INTO account (account_id, customer_id, account_number, account_type, "
        "balance_cents, account_status, open_date, close_date, as_of_date) "
        "VALUES ('A9', 'C1', '9', 'DEPOSIT', 0, 'CLOSED', '2020-01-01', '2019-01-01', "
        "'2026-01-01')",
    ),
    (
        "invalid asset_allocation JSON",
        "INSERT INTO investment (account_id, portfolio_value_cents, asset_allocation) "
        "VALUES ('A1', 100, '{not json')",
    ),
    (
        "share_bps above 100%",
        "INSERT INTO beneficiary (beneficiary_id, account_id, beneficiary_name, share_bps, "
        "as_of_date) VALUES ('B9', 'A1', 'Someone', 10001, '2026-01-01')",
    ),
    (
        "self-referential customer relationship",
        "INSERT INTO customer_relationship (relationship_id, from_customer_id, to_customer_id, "
        "relationship_type, source_system, as_of_date) "
        "VALUES ('R9', 'C1', 'C1', 'SPOUSE', 'CORE', '2026-01-01')",
    ),
    (
        "inferred relationship without a confidence score",
        "INSERT INTO customer_relationship (relationship_id, from_customer_id, to_customer_id, "
        "relationship_type, is_inferred, source_system, as_of_date) "
        "VALUES ('R9', 'C1', 'C2', 'SPOUSE', 1, 'CORE', '2026-01-01')",
    ),
    (
        "inferred life event without cited signals",
        "INSERT INTO life_event (life_event_id, customer_id, life_event_type, event_date, "
        "confidence, source, is_inferred, as_of_date) "
        "VALUES ('L9', 'C1', 'MARRIAGE', '2024-06-01', 0.8, 'INFERRED', 1, '2026-01-01')",
    ),
    (
        "life event whose source and is_inferred disagree",
        "INSERT INTO life_event (life_event_id, customer_id, life_event_type, event_date, "
        "source, is_inferred, as_of_date) "
        "VALUES ('L9', 'C1', 'MARRIAGE', '2024-06-01', 'SYSTEM_OF_RECORD', 1, '2026-01-01')",
    ),
    (
        "decided application with no decision date",
        "INSERT INTO application (application_id, customer_id, product_applied, event_date, "
        "application_status, as_of_date, source_system) "
        "VALUES ('AP9', 'C1', 'Card', '2024-01-01', 'APPROVED', '2026-01-01', 'ORIG')",
    ),
    (
        "acceptance probability above 1",
        "INSERT INTO customer_offer (customer_offer_id, customer_id, offer_id, event_date, "
        "acceptance_probability, probability_confidence, as_of_date) "
        "VALUES ('CO9', 'C1', 'O1', '2025-01-01', 1.5, 'HIGH', '2026-01-01')",
    ),
    (
        "member_count below one",
        "INSERT INTO household (household_id, household_name, address_hash, member_count, "
        "as_of_date) VALUES ('H9', 'X', 'hash', 0, '2026-01-01')",
    ),
)


@pytest.mark.parametrize(
    ("description", "sql"), REJECTED_INSERTS, ids=[d for d, _ in REJECTED_INSERTS]
)
def test_check_constraints_reject_bad_data(writer: Engine, description: str, sql: str) -> None:
    """Requirement 14.5 in executable form: a record failing its schema is rejected, not stored."""
    with writer.begin() as connection:
        _seed_customer(connection)
    with writer.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text(sql))


def test_card_last4_must_match_the_pan(writer: Engine) -> None:
    """Two copies of one fact must not disagree; design §4.3 stores both deliberately."""
    with writer.begin() as connection:
        _seed_customer(connection)
        connection.execute(
            text(
                "INSERT INTO account (account_id, customer_id, account_number, account_type, "
                "balance_cents, account_status, open_date, as_of_date) "
                "VALUES ('A1', 'C1', '0001', 'CARD', 0, 'ACTIVE', '2020-01-01', '2026-01-01')"
            )
        )

    with writer.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO credit_card (account_id, card_number, card_last4, credit_limit_cents) "
                "VALUES ('A1', '4111111111111111', '9999', 500000)"
            )
        )

    with writer.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO credit_card (account_id, card_number, card_last4, credit_limit_cents) "
                "VALUES ('A1', '4111111111111111', '1111', 500000)"
            )
        )
    with writer.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM credit_card")).scalar_one() == 1


def test_valid_rows_are_accepted(writer: Engine) -> None:
    """The constraint tests would also pass if every insert failed. This is the control."""
    with writer.begin() as connection:
        _seed_customer(connection)
        connection.execute(
            text(
                "INSERT INTO financial_profile (customer_id, total_assets_cents, "
                "total_liabilities_cents, net_worth_cents, as_of_date) "
                "VALUES ('C1', 5000000, 2000000, 3000000, '2026-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO risk_profile (customer_id, delinquency_status, "
                "current_days_past_due, as_of_date) "
                "VALUES ('C1', 'DPD_30_59', 45, '2026-01-01')"
            )
        )
    with writer.connect() as connection:
        assert (
            connection.execute(text("SELECT net_worth_cents FROM financial_profile")).scalar_one()
            == 3000000
        )


def test_a_missing_alembic_ini_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Alembic's own error for this is a bare KeyError from config parsing.

    Worth naming because the realistic cause is running from a working directory the packaging did
    not account for, and "alembic.ini not found at <path>" says that where a KeyError does not.
    """
    monkeypatch.setattr(migrations_module, "ALEMBIC_INI", tmp_path / "absent.ini")
    with pytest.raises(MigrationError, match=re.escape("alembic.ini not found")):
        migrations_module.alembic_config(tmp_path / "customer.db")


def test_an_empty_migration_set_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``head_revision`` backs the readiness schema check, so "none" must not read as ``None``."""
    empty = tmp_path / "migrations"
    (empty / "versions").mkdir(parents=True)
    (empty / "env.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(migrations_module, "MIGRATIONS_DIR", empty)

    with pytest.raises(MigrationError, match="no migrations found"):
        migrations_module.head_revision()
