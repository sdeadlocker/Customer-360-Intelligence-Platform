"""Synthetic data generator tests (Phase 2).

Organized around the Phase 2 gate rather than around the module layout, because the gate is what the
phase is for: *``c360 seed`` produces 100 customers with every cohort present, ~350 accounts and
~20k
transactions in seconds; reproducibility test green; ``--count 1000`` also works.*

Two notes on how these are written.

**The reproducibility test compares database files, not row counts.** Task 2.1 says "byte-identical
databases", and the failure modes it is guarding against — a ``set`` of strings iterated somewhere,
a
``hash()`` call, a stray ``datetime.now()`` — all produce identical *counts* and different
*content*. It
also runs the two generations in separate subprocesses, because ``PYTHONHASHSEED`` is fixed for the
lifetime of a process: two in-process runs would share the same salt and a hash-order bug would
survive
the test.

**Volume assertions are ranges, not equalities.** The counts are emergent from a distribution, so
pinning
them exactly would make every future tuning change a test failure with no signal in it. The ranges
are
wide enough to permit tuning and tight enough to catch a cohort collapsing or a stage not running.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from c360.cli import main as cli_main
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.migrations import upgrade_database
from c360.domain.dates import is_iso_date
from c360.generator import adversarial
from c360.generator.cohorts import COHORT_PROFILES, MIN_COHORT_SIZE, Cohort, allocate
from c360.generator.context import DEFAULT_AS_OF, GeneratorContext, add_months, stable_hash
from c360.generator.loader import load
from c360.generator.pipeline import DatabaseExistsError, generate, seed
from c360.generator.tables import LOAD_ORDER, Dataset, RowArityError, Table

#: The default dataset size, per requirement 14.1.
DEFAULT_COUNT = 100
DEFAULT_SEED = 42


# ==================================================================== cohort allocation
class TestCohortAllocation:
    """Task 2.1: the persona distribution and its per-cohort floor."""

    def test_shares_sum_to_the_whole(self):
        assert sum(profile.share_bps for profile in COHORT_PROFILES) == 10_000

    def test_allocation_sums_to_the_requested_count(self):
        for count in (8, 24, 100, 137, 1_000, 5_000):
            allocation = allocate(count)
            assert sum(allocation.values()) == count, count

    def test_every_cohort_is_present_at_the_default_size(self):
        allocation = allocate(DEFAULT_COUNT)
        assert set(allocation) == set(Cohort)
        assert all(size >= MIN_COHORT_SIZE for size in allocation.values())

    def test_the_floor_is_what_makes_the_eval_panel_buildable(self):
        """Design §14.2 needs >= 3 per cohort at 100 customers; proportional alone gives 1 and 2."""
        allocation = allocate(DEFAULT_COUNT)
        assert allocation[Cohort.ISOLATED] >= 3
        assert allocation[Cohort.FRAUD_FLAGGED] >= 3

    def test_the_deficit_is_paid_by_the_largest_cohort(self):
        """Proportions must survive the floor: mass market pays, other cohorts are untouched."""
        allocation = allocate(DEFAULT_COUNT)
        assert allocation[Cohort.AFFLUENT] == 25
        assert allocation[Cohort.HNW] == 10
        assert allocation[Cohort.SMALL_BUSINESS] == 8
        # 45 proportional, less the 3 needed to lift fraud (2->3) and isolated (1->3).
        assert allocation[Cohort.MASS_MARKET] == 42

    def test_floor_degrades_rather_than_failing_on_a_small_count(self):
        allocation = allocate(16)
        assert sum(allocation.values()) == 16
        assert all(size >= 1 for size in allocation.values())

    def test_a_count_below_one_per_cohort_is_rejected(self):
        with pytest.raises(ValueError, match="at least 8"):
            allocate(7)

    def test_allocation_is_deterministic(self):
        assert allocate(DEFAULT_COUNT) == allocate(DEFAULT_COUNT)


# ==================================================================== context primitives
class TestContext:
    """The seeded random source and the helpers that must not leak process state."""

    def test_stable_hash_does_not_use_pythons_salted_hash(self):
        """A subprocess has a different PYTHONHASHSEED; the digest must not move with it."""
        expected = stable_hash("4820 Kestrel Lane", "Columbus", "OH", "43215")
        code = (
            "from c360.generator.context import stable_hash;"
            "print(stable_hash('4820 Kestrel Lane','Columbus','OH','43215'))"
        )
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == expected

    def test_add_months_clamps_to_a_real_calendar_date(self):
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
        assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
        assert add_months(date(2026, 3, 15), -3) == date(2025, 12, 15)

    def test_history_window_is_24_whole_months(self):
        ctx = GeneratorContext.create(count=1, seed=1)
        months = ctx.history_months()
        assert len(months) == 24
        assert all(month.day == 1 for month in months)
        assert months[0] == ctx.history_start
        assert months[-1] < ctx.as_of or months[-1].month == ctx.as_of.month

    def test_two_contexts_at_one_seed_draw_the_same_values(self):
        first = GeneratorContext.create(count=10, seed=7)
        second = GeneratorContext.create(count=10, seed=7)
        assert [first.integer((0, 10_000)) for _ in range(50)] == [
            second.integer((0, 10_000)) for _ in range(50)
        ]

    def test_entity_ids_sort_lexicographically_in_numeric_order(self):
        ids = [GeneratorContext.entity_id("C", index) for index in (9, 10, 100)]
        assert ids == sorted(ids)

    def test_day_in_month_never_produces_an_impossible_date(self):
        ctx = GeneratorContext.create(count=1, seed=3)
        for _ in range(200):
            result = ctx.day_in_month(date(2026, 2, 1), low=1, high=31)
            assert result.month == 2
            assert result.day <= 28


# ==================================================================== dataset plumbing
class TestDataset:
    """The arity guard that keeps positional rows aligned with declared columns."""

    def test_row_arity_is_enforced(self):
        dataset = Dataset()
        table = Table("employer", ("a", "b"))
        with pytest.raises(RowArityError, match="takes 2 values, got 1"):
            dataset.add(table, ("only",))

    def test_insert_sql_matches_the_column_count(self):
        for table in LOAD_ORDER:
            statement = table.insert_sql
            assert statement.count("?") == len(table.columns), table.name
            assert statement.startswith(f"INSERT INTO {table.name} (")

    def test_load_order_puts_assets_before_loans(self):
        """loan.collateral_asset_id references asset, so the design's prose order would fail."""
        names = [table.name for table in LOAD_ORDER]
        assert names.index("asset") < names.index("loan")
        assert names.index("account") < names.index("loan")
        assert names.index("customer") < names.index("account")
        assert names.index("household") < names.index("customer")


# ==================================================================== the seeded database
@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated, generated and loaded database at the default size."""
    path = tmp_path_factory.mktemp("generator") / "customer.db"
    seed(database=path, count=DEFAULT_COUNT, seed=DEFAULT_SEED)
    return path


@pytest.fixture
def engine(seeded: Path) -> Iterator[Engine]:
    """Read-only, as the application opens it."""
    created = create_sqlite_engine(seeded, mode=AccessMode.READ_ONLY, pool_size=2)
    try:
        yield created
    finally:
        created.dispose()


def _scalar(engine: Engine, sql: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(sql)).scalar_one())


def _rows(engine: Engine, sql: str) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql)).all()]


class TestPhaseGateVolumes:
    """The Phase 2 gate's stated volumes (design §15)."""

    def test_customer_count_is_exact(self, engine: Engine):
        assert _scalar(engine, "SELECT count(*) FROM customer") == DEFAULT_COUNT

    def test_every_cohort_is_represented(self, engine: Engine):
        """Cohorts are not stored, so they are checked through their observable consequences."""
        # Thin file: no bureau row at all.
        assert _scalar(engine, "SELECT count(*) FROM customer") > _scalar(
            engine, "SELECT count(*) FROM credit_profile"
        )
        # Delinquent: DPD buckets beyond the grace band.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM risk_profile WHERE delinquency_status IN "
                "('DPD_30_59','DPD_60_89','DPD_90_PLUS')",
            )
            >= 3
        )
        # Fraud flagged: AML or PEP.
        assert (
            _scalar(engine, "SELECT count(*) FROM risk_profile WHERE aml_flag = 1 OR pep_flag = 1")
            >= 3
        )
        # Isolated: no household.
        assert _scalar(engine, "SELECT count(*) FROM customer WHERE household_id IS NULL") >= 3
        # HNW / UHNW segments.
        assert (
            _scalar(
                engine, "SELECT count(*) FROM customer WHERE customer_segment IN ('HNW','UHNW')"
            )
            >= 3
        )
        assert (
            _scalar(
                engine, "SELECT count(*) FROM customer WHERE customer_segment = 'SMALL_BUSINESS'"
            )
            >= 3
        )

    def test_account_volume_is_about_350(self, engine: Engine):
        assert 280 <= _scalar(engine, "SELECT count(*) FROM account") <= 450

    def test_transaction_volume_is_about_20k(self, engine: Engine):
        assert 16_000 <= _scalar(engine, "SELECT count(*) FROM txn") <= 26_000

    def test_household_volume_is_about_35(self, engine: Engine):
        assert 25 <= _scalar(engine, "SELECT count(*) FROM household") <= 50

    def test_engagement_event_volume_is_about_1500(self, engine: Engine):
        assert 1_000 <= _scalar(engine, "SELECT count(*) FROM customer_event") <= 2_400

    def test_every_table_in_the_load_order_received_rows(self, engine: Engine):
        """A stage that silently did nothing is the failure this catches."""
        for table in LOAD_ORDER:
            count = _scalar(engine, f"SELECT count(*) FROM {table.name}")  # noqa: S608
            assert count > 0, f"{table.name} is empty"


class TestReferentialAndSchemaIntegrity:
    """Nothing generated may violate a constraint the application relies on."""

    def test_foreign_keys_are_satisfied(self, engine: Engine):
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    def test_the_database_passes_an_integrity_check(self, engine: Engine):
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"

    def test_every_date_column_holds_extended_iso_format(self, engine: Engine):
        """The CHECK constraints use `IS date(x)`; this asserts the Python parser agrees."""
        checks = (
            ("customer", "customer_since"),
            ("customer", "date_of_birth"),
            ("account", "open_date"),
            ("account", "close_date"),
            ("txn", "transaction_date"),
            ("life_event", "event_date"),
            ("application", "event_date"),
            ("application", "decision_date"),
            ("customer_offer", "reaction_date"),
            ("asset", "acquired_date"),
        )
        for table, column in checks:
            values = _rows(
                engine,
                f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL",  # noqa: S608
            )
            assert values, f"{table}.{column} produced no values to check"
            for (value,) in values:
                assert is_iso_date(value), f"{table}.{column} = {value!r}"

    def test_net_worth_equals_assets_minus_liabilities(self, engine: Engine):
        """The financial_profile CHECK, asserted independently of the database enforcing it."""
        mismatches = _scalar(
            engine,
            "SELECT count(*) FROM financial_profile "
            "WHERE net_worth_cents <> total_assets_cents - total_liabilities_cents",
        )
        assert mismatches == 0

    def test_delinquency_status_agrees_with_days_past_due(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM risk_profile WHERE "
                "(delinquency_status = 'CURRENT' AND current_days_past_due <> 0) OR "
                "(delinquency_status = 'DPD_30_59' AND current_days_past_due"
                " NOT BETWEEN 30 AND 59)",
            )
            == 0
        )

    def test_card_last4_agrees_with_the_pan(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM credit_card WHERE card_last4 <> substr(card_number, -4)",
            )
            == 0
        )

    def test_closed_accounts_and_close_dates_are_paired(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM account WHERE "
                "(account_status = 'CLOSED') <> (close_date IS NOT NULL)",
            )
            == 0
        )

    def test_investment_components_sum_to_the_portfolio_value(self, engine: Engine):
        """Cents.allocate guarantees this; a drifting component split would be a real defect."""
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM investment WHERE portfolio_value_cents <> "
                "mutual_funds_cents + stocks_cents + bonds_cents + retirement_accounts_cents",
            )
            == 0
        )

    def test_asset_allocation_is_valid_json(self, engine: Engine):
        rows = _rows(engine, "SELECT asset_allocation FROM investment")
        assert rows
        for (payload,) in rows:
            assert isinstance(payload, str)
            assert isinstance(json.loads(payload), dict)


class TestRequirementSpecificShapes:
    """Data states that a later phase's acceptance criteria depend on existing."""

    def test_inferred_relationships_carry_confidence_and_a_basis(self, engine: Engine):
        """Requirement 6.6: an inferred link renders differently, so it needs both fields."""
        assert (
            _scalar(engine, "SELECT count(*) FROM customer_relationship WHERE is_inferred = 1") > 0
        )
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM customer_relationship WHERE is_inferred = 1 "
                "AND (confidence IS NULL OR inference_basis IS NULL)",
            )
            == 0
        )

    def test_both_inferred_and_system_of_record_relationships_exist(self, engine: Engine):
        assert (
            _scalar(engine, "SELECT count(*) FROM customer_relationship WHERE is_inferred = 0") > 0
        )

    def test_inferred_life_events_cite_signals(self, engine: Engine):
        """Requirement 7.3: an inferred event must cite what it was inferred from."""
        inferred = _scalar(engine, "SELECT count(*) FROM life_event WHERE is_inferred = 1")
        assert inferred > 0
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM life_event WHERE is_inferred = 1 "
                "AND (confidence IS NULL OR signals IS NULL OR signals = '[]')",
            )
            == 0
        )

    def test_life_event_signals_reference_transactions_that_exist(self, engine: Engine):
        """The corroboration of design §15: the citation has to resolve."""
        rows = _rows(
            engine,
            "SELECT signals FROM life_event WHERE is_inferred = 1 AND signals IS NOT NULL",
        )
        assert rows
        transaction_ids = {row[0] for row in _rows(engine, "SELECT transaction_id FROM txn")}
        resolved = 0
        for (payload,) in rows:
            for signal in json.loads(str(payload)):
                # Salary-backed events cite a month marker rather than a row; both are legitimate.
                if "-SALARY-" in signal:
                    continue
                assert signal in transaction_ids, signal
                resolved += 1
        assert resolved > 0

    def test_suppressed_offers_record_a_reason(self, engine: Engine):
        """Requirement 9.5: the reason must be visible, so it cannot be null."""
        assert _scalar(engine, "SELECT count(*) FROM customer_offer WHERE is_suppressed = 1") > 0
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM customer_offer WHERE is_suppressed = 1 "
                "AND suppression_reason IS NULL",
            )
            == 0
        )

    def test_suppression_matches_an_actually_held_product(self, engine: Engine):
        """Suppression has to be a fact about the customer, not a coin flip."""
        leaks = _scalar(
            engine,
            """
            SELECT count(*) FROM customer_offer co
            JOIN offer o ON o.offer_id = co.offer_id
            WHERE co.is_suppressed = 1
              AND NOT EXISTS (
                SELECT 1 FROM account a
                WHERE a.customer_id = co.customer_id
                  AND a.product_code = o.product_code
                  AND a.account_status <> 'CLOSED'
              )
            """,
        )
        assert leaks == 0

    def test_pending_offers_have_no_reaction_date(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM customer_offer WHERE "
                "(customer_reaction = 'PENDING') <> (reaction_date IS NULL)",
            )
            == 0
        )

    def test_expected_value_follows_value_times_probability(self, engine: Engine):
        """Requirement 9.2: the ranking number must be reproducible from its own inputs."""
        rows = _rows(
            engine,
            """
            SELECT co.expected_value_cents, o.value_cents, co.acceptance_probability
            FROM customer_offer co JOIN offer o ON o.offer_id = co.offer_id
            WHERE co.acceptance_probability IS NOT NULL
            """,
        )
        assert rows
        for expected, value, probability in rows:
            recomputed = int(value) * round(float(probability) * 10_000) // 10_000
            assert abs(int(expected) - recomputed) <= 1

    def test_declined_offers_straddle_the_cooling_off_window(self, engine: Engine):
        """Requirement 9.6 needs both a decline inside 90 days and one outside it."""
        inside = _scalar(
            engine,
            "SELECT count(*) FROM customer_offer WHERE customer_reaction = 'DECLINED' "
            "AND julianday('2026-09-01') - julianday(reaction_date) <= 90",
        )
        outside = _scalar(
            engine,
            "SELECT count(*) FROM customer_offer WHERE customer_reaction = 'DECLINED' "
            "AND julianday('2026-09-01') - julianday(reaction_date) > 90",
        )
        assert inside > 0
        assert outside > 0

    def test_decided_applications_have_a_decision_date(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM application WHERE "
                "(application_status IN ('APPROVED','DECLINED','FUNDED')) "
                "<> (decision_date IS NOT NULL)",
            )
            == 0
        )

    def test_the_fraud_cohort_has_failed_applications(self, engine: Engine):
        """Design §15: fraud-flagged customers get failed applications."""
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM application WHERE fraud_result = 'FAIL' "
                "AND application_status = 'DECLINED'",
            )
            > 0
        )

    def test_thin_file_customers_exist_with_absent_rather_than_empty_fields(self, engine: Engine):
        """Requirement 4.7 distinguishes no value from a blank, so the NULLs have to be real."""
        assert _scalar(engine, "SELECT count(*) FROM customer WHERE date_of_birth IS NULL") > 0
        assert _scalar(engine, "SELECT count(*) FROM customer WHERE occupation IS NULL") > 0
        # And at least one customer with no contact row at all.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM customer c WHERE NOT EXISTS "
                "(SELECT 1 FROM contact_info ci WHERE ci.customer_id = c.customer_id)",
            )
            > 0
        )

    def test_no_generated_contact_detail_could_reach_a_real_person(self, engine: Engine):
        """Emails use the reserved .invalid TLD and numbers the 555-01xx fiction block."""
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM contact_info WHERE email IS NOT NULL "
                "AND email NOT LIKE '%@example.invalid'",
            )
            == 0
        )
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM contact_info WHERE phone_number IS NOT NULL "
                "AND phone_number NOT LIKE '%555-01%'",
            )
            == 0
        )

    def test_closed_accounts_hold_no_balance(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM account WHERE account_status = 'CLOSED' "
                "AND balance_cents <> 0",
            )
            == 0
        )

    def test_transactions_are_signed_by_direction(self, engine: Engine):
        """Debits negative, credits positive — the convention c360.domain.money documents."""
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM txn WHERE transaction_type = 'DEBIT' AND amount_cents > 0",
            )
            == 0
        )
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM txn WHERE transaction_type = 'CREDIT' AND amount_cents < 0",
            )
            == 0
        )

    def test_transactions_fall_inside_the_24_month_window(self, engine: Engine):
        earliest, latest = _rows(
            engine, "SELECT min(transaction_date), max(transaction_date) FROM txn"
        )[0]
        assert str(earliest) >= "2024-09-01"
        assert str(latest) <= "2026-09-30"

    def test_spend_categories_are_varied_enough_for_analytics(self, engine: Engine):
        """Requirement 5.9 aggregates by category; one category would make that trivial."""
        assert _scalar(engine, "SELECT count(DISTINCT transaction_category) FROM txn") >= 12

    def test_seasonality_is_visible_in_december_shopping(self, engine: Engine):
        """A flat spend curve would make the trend chart and the deviation flag meaningless."""
        december = _scalar(
            engine,
            "SELECT coalesce(-sum(amount_cents), 0) FROM txn WHERE transaction_category = "
            "'SHOPPING' AND strftime('%m', transaction_date) = '12'",
        )
        february = _scalar(
            engine,
            "SELECT coalesce(-sum(amount_cents), 0) FROM txn WHERE transaction_category = "
            "'SHOPPING' AND strftime('%m', transaction_date) = '02'",
        )
        assert december > february

    def test_joint_account_shares_sum_to_the_whole(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM (SELECT account_id, sum(ownership_bps) AS total "
                "FROM account_party GROUP BY account_id) WHERE total <> 10000",
            )
            == 0
        )

    def test_joint_accounts_exist(self, engine: Engine):
        assert _scalar(engine, "SELECT count(*) FROM account_party WHERE party_role = 'JOINT'") > 0

    def test_beneficiary_shares_sum_to_the_whole_per_account(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM (SELECT account_id, sum(share_bps) AS total "
                "FROM beneficiary GROUP BY account_id) WHERE total <> 10000",
            )
            == 0
        )

    def test_beneficiaries_exist_who_are_not_customers(self, engine: Engine):
        """The nullable beneficiary_customer_id is a real case, not a schema accident."""
        assert (
            _scalar(
                engine, "SELECT count(*) FROM beneficiary WHERE beneficiary_customer_id IS NULL"
            )
            > 0
        )

    def test_mortgages_are_secured_on_a_property(self, engine: Engine):
        """collateral_asset_id is why asset loads before loan; this proves the link resolves."""
        unsecured = _scalar(
            engine,
            "SELECT count(*) FROM loan WHERE loan_type = 'MORTGAGE'"
            " AND collateral_asset_id IS NULL",
        )
        assert unsecured == 0
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM loan l JOIN asset a ON a.asset_id = l.collateral_asset_id "
                "WHERE a.asset_type = 'PROPERTY' AND a.is_collateral = 1",
            )
            > 0
        )

    def test_a_loan_never_predates_its_collateral(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM loan l JOIN asset a ON a.asset_id = l.collateral_asset_id "
                "WHERE l.loan_start_date < a.acquired_date",
            )
            == 0
        )

    def test_household_member_counts_match_the_membership_table(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM household h WHERE h.member_count <> "
                "(SELECT count(*) FROM household_member m WHERE m.household_id = h.household_id)",
            )
            == 0
        )

    def test_each_household_has_exactly_one_head(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM (SELECT household_id, count(*) AS heads"
                " FROM household_member "
                "WHERE member_role = 'HEAD' GROUP BY household_id) WHERE heads <> 1",
            )
            == 0
        )

    def test_no_household_member_is_older_than_their_parent(self, engine: Engine):
        """Roles are derived from age; a child born before the head would read as a bug."""
        assert (
            _scalar(
                engine,
                """
                SELECT count(*) FROM customer_relationship r
                JOIN customer parent ON parent.customer_id = r.from_customer_id
                JOIN customer child  ON child.customer_id  = r.to_customer_id
                WHERE r.relationship_type = 'PARENT'
                  AND parent.date_of_birth IS NOT NULL
                  AND child.date_of_birth IS NOT NULL
                  AND child.date_of_birth <= parent.date_of_birth
                """,
            )
            == 0
        )

    def test_nobody_opened_an_account_before_turning_18(self, engine: Engine):
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM customer WHERE date_of_birth IS NOT NULL "
                "AND (julianday(customer_since) - julianday(date_of_birth)) / 365.25 < 18",
            )
            == 0
        )

    def test_isolated_customers_have_no_relationships_or_offers(self, engine: Engine):
        """Design §15's isolated cohort makes the empty graph and empty offers states real."""
        isolated = _rows(engine, "SELECT customer_id FROM customer WHERE household_id IS NULL")
        assert isolated
        ids = ", ".join(f"'{row[0]}'" for row in isolated)
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM customer_relationship WHERE "  # noqa: S608
                f"from_customer_id IN ({ids}) OR to_customer_id IN ({ids})",
            )
            == 0
        )
        assert (
            _scalar(
                engine,
                f"SELECT count(*) FROM customer_offer WHERE customer_id IN ({ids})",  # noqa: S608
            )
            == 0
        )

    def test_the_graph_is_deep_enough_for_three_hop_traversal(self, engine: Engine):
        """Requirement 6.7 puts a budget on 3-hop traversal; a shallow graph cannot exercise it."""
        depth = _scalar(
            engine,
            """
            WITH RECURSIVE reach(start_id, node_id, hops) AS (
              SELECT from_customer_id, to_customer_id, 1 FROM customer_relationship
              UNION
              SELECT r.start_id, e.to_customer_id, r.hops + 1
              FROM reach r JOIN customer_relationship e ON e.from_customer_id = r.node_id
              WHERE r.hops < 3
            )
            SELECT count(*) FROM reach WHERE hops = 3
            """,
        )
        assert depth > 0


class TestAdversarialSeed:
    """Task 2.7: the payloads Phase 11.5 will assert containment against."""

    def test_the_manifest_covers_every_registered_probe(self):
        manifest = adversarial.probe_manifest()
        assert len(manifest) == len(adversarial.ADVERSARIAL_PROBES)
        for entry in manifest:
            assert entry["probe_id"]
            assert entry["text"]
            assert entry["must_not"]

    def test_carrier_selection_is_sparse_and_deterministic(self):
        carriers = [
            index
            for index in range(1, DEFAULT_COUNT + 1)
            if adversarial.is_probe_carrier_index(index)
        ]
        assert carriers == [29, 58, 87]

    def test_the_employer_payload_is_present_in_the_database(self, engine: Engine):
        names = _rows(
            engine,
            "SELECT employer_name FROM employer WHERE employer_id = "  # noqa: S608
            f"'{adversarial.ADVERSARIAL_EMPLOYER_ID}'",
        )
        assert len(names) == 1
        assert "ADMIN OVERRIDE" in str(names[0][0])

    def test_a_merchant_payload_reached_the_transaction_table(self, engine: Engine):
        planted = _scalar(
            engine,
            "SELECT count(*) FROM txn WHERE merchant LIKE '%ignore all previous instructions%' "
            "OR merchant LIKE '%disregard your citation rules%'",
        )
        assert planted > 0

    def test_a_service_note_payload_reached_the_event_table(self, engine: Engine):
        planted = _scalar(
            engine,
            "SELECT count(*) FROM customer_event WHERE notes LIKE '%new instructions follow%' "
            "OR notes LIKE '%ignore any redaction filter%'",
        )
        assert planted > 0

    def test_payloads_stay_a_small_minority_of_free_text(self, engine: Engine):
        """A dataset that is mostly attack payloads is not a containment test."""
        total = _scalar(engine, "SELECT count(*) FROM customer_event WHERE notes IS NOT NULL")
        planted = _scalar(
            engine,
            "SELECT count(*) FROM customer_event WHERE notes LIKE '%new instructions follow%' "
            "OR notes LIKE '%ignore any redaction filter%'",
        )
        assert planted * 10 < total


class TestReproducibility:
    """Task 2.1: two runs at one seed produce byte-identical databases."""

    def test_same_seed_produces_identical_bytes_across_processes(self, tmp_path: Path):
        """Separate processes, because PYTHONHASHSEED is fixed for a process's lifetime.

        An in-process comparison would share one hash salt, so a bug that leaked ``set`` ordering
        into
        the output would produce the same bytes twice and the test would pass while testing nothing.
        """
        first = tmp_path / "first.db"
        second = tmp_path / "second.db"
        for target in (first, second):
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-m",
                    "c360",
                    "seed",
                    "--count",
                    "40",
                    "--seed",
                    "42",
                    "--database",
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr

        assert first.read_bytes() == second.read_bytes()

    def test_a_different_seed_produces_a_different_database(self, tmp_path: Path):
        _, first = generate(count=40, seed=42)
        _, second = generate(count=40, seed=43)
        assert first.rows["customer"] != second.rows["customer"]

    def test_generation_is_identical_in_process_at_one_seed(self):
        _, first = generate(count=40, seed=42)
        _, second = generate(count=40, seed=42)
        assert first.rows == second.rows

    def test_the_as_of_date_is_fixed_rather_than_today(self):
        """Seeding from today would make the same seed produce a different database tomorrow."""
        population, _ = generate(count=16, seed=42)
        assert all(plan.customer_since <= DEFAULT_AS_OF for plan in population.customers)
        _, dataset = generate(count=16, seed=42, as_of=DEFAULT_AS_OF)
        as_of_values = {row[16] for row in dataset.rows["customer"]}
        assert as_of_values == {DEFAULT_AS_OF.isoformat()}


class TestScaling:
    """The Phase 2 gate also requires --count 1000 to work."""

    def test_a_thousand_customers_generates_without_error(self):
        population, dataset = generate(count=1_000, seed=DEFAULT_SEED)
        assert len(population.customers) == 1_000
        counts = dataset.counts()
        assert counts["customer"] == 1_000
        # Volumes should scale roughly linearly with the population.
        assert 150_000 <= counts["txn"] <= 300_000
        assert 2_800 <= counts["account"] <= 4_500
        assert all(size >= MIN_COHORT_SIZE for size in population.cohort_counts().values())

    def test_the_minimum_viable_population_contains_every_cohort(self):
        population, _ = generate(count=8, seed=DEFAULT_SEED)
        assert len(population.customers) == 8
        assert all(size >= 1 for size in population.cohort_counts().values())


class TestSeedingBehaviour:
    """The safety and reporting behaviour of the seeding entry point."""

    def test_seeding_refuses_to_overwrite_without_force(self, tmp_path: Path):
        target = tmp_path / "customer.db"
        seed(database=target, count=8, seed=1)
        with pytest.raises(DatabaseExistsError, match="already exists"):
            seed(database=target, count=8, seed=1)

    def test_force_replaces_an_existing_database(self, tmp_path: Path):
        target = tmp_path / "customer.db"
        seed(database=target, count=8, seed=1)
        report = seed(database=target, count=12, seed=1, force=True)
        assert report.customer_count == 12

    def test_the_report_summarizes_cohorts_and_tables(self, tmp_path: Path):
        report = seed(database=tmp_path / "customer.db", count=DEFAULT_COUNT, seed=DEFAULT_SEED)
        assert report.customer_count == DEFAULT_COUNT
        assert sum(report.cohort_counts.values()) == DEFAULT_COUNT
        assert report.load.total_rows > 10_000
        text_summary = "\n".join(report.summary_lines())
        assert "cohorts:" in text_summary
        assert "MASS_MARKET" in text_summary

    def test_loading_into_an_unmigrated_database_fails_loudly(self, tmp_path: Path):
        """Silently creating the schema here would give the seeder a second definition of it."""
        _, dataset = generate(count=8, seed=1)
        with pytest.raises(Exception, match="no such table"):
            load(dataset, tmp_path / "empty.db")

    def test_a_migrated_but_unseeded_database_is_a_valid_starting_point(self, tmp_path: Path):
        target = tmp_path / "customer.db"
        upgrade_database(target)
        assert target.is_file()
        engine = create_sqlite_engine(target, mode=AccessMode.READ_ONLY, pool_size=1)
        try:
            assert _scalar(engine, "SELECT count(*) FROM customer") == 0
        finally:
            engine.dispose()


class TestCli:
    """Task 2.8's command surface."""

    def test_seed_command_reports_a_summary(self, tmp_path: Path):
        code = cli_main(
            [
                "seed",
                "--count",
                "8",
                "--seed",
                "5",
                "--database",
                str(tmp_path / "cli.db"),
            ]
        )
        assert code == 0
        assert (tmp_path / "cli.db").is_file()

    def test_an_existing_database_is_an_error_without_force(self, tmp_path: Path):
        target = tmp_path / "cli.db"
        assert cli_main(["seed", "--count", "8", "--seed", "5", "--database", str(target)]) == 0
        assert cli_main(["seed", "--count", "8", "--seed", "5", "--database", str(target)]) == 1

    def test_a_count_that_cannot_hold_every_cohort_is_an_error(self, tmp_path: Path):
        code = cli_main(
            ["seed", "--count", "3", "--seed", "5", "--database", str(tmp_path / "cli.db")]
        )
        assert code == 1

    def test_an_invalid_as_of_date_is_rejected(self, tmp_path: Path):
        with pytest.raises(SystemExit):
            cli_main(
                [
                    "seed",
                    "--as-of",
                    "2026-9-1",
                    "--database",
                    str(tmp_path / "cli.db"),
                ]
            )
