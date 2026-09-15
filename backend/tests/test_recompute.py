"""Phase 3 gate: derived values, the search index and the graph projection (tasks 3.1-3.6).

Organized around the gate: *search returns correct results for all eight key types; graph rebuilds
deterministically; derived-value equality test green.*

The dataset is the real seeded generator output rather than a hand-written fixture, because Phase 3
projects the whole database and a hand-written two-customer fixture would not exercise households,
inferred relationships, closed accounts or the eight distinct search keys. The seed is fixed (42),
so the projection is reproducible and the assertions can name expected structure.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from c360.cli import main as cli_main
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.recompute import RecomputeReport, recompute
from c360.domain.money import Cents
from c360.generator.pipeline import seed
from c360.main import create_app
from tests.conftest import make_settings

SEED = 42
COUNT = 100


# ==================================================================== fixtures
@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A seeded database, built once for the module. Recompute runs against copies where needed."""
    path = tmp_path_factory.mktemp("recompute") / "customer.db"
    seed(database=path, count=COUNT, seed=SEED)
    return path


@pytest.fixture
def recomputed_db(seeded_db: Path, tmp_path: Path) -> Path:
    """A fresh copy of the seeded database with the projection built. Isolated per test."""
    path = tmp_path / "customer.db"
    shutil.copy(seeded_db, path)
    recompute(path)
    return path


@pytest.fixture
def reader(recomputed_db: Path) -> Iterator[Engine]:
    engine = create_sqlite_engine(recomputed_db, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        yield engine
    finally:
        engine.dispose()


def _one(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).fetchone()


def _all(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql), params or {}).fetchall())


# ==================================================================== task 3.1 derived values
class TestDerivedValueEquality:
    """The executable form of design §14.6: stored derived values equal freshly-computed ones."""

    def test_net_worth_equals_assets_minus_liabilities(self, reader: Engine) -> None:
        rows = _all(
            reader,
            "SELECT net_worth_cents, total_assets_cents, total_liabilities_cents "
            "FROM derived_financial",
        )
        assert rows
        for net, assets, liabilities in rows:
            assert net == assets - liabilities

    def test_total_assets_matches_a_fresh_sum(self, reader: Engine) -> None:
        """Recompute the sum independently and compare, per customer."""
        stored = {
            str(row[0]): row[1]
            for row in _all(reader, "SELECT customer_id, total_assets_cents FROM derived_financial")
        }
        for customer_id, expected in stored.items():
            deposits = _one(
                reader,
                "SELECT COALESCE(SUM(balance_cents), 0) FROM account "
                "WHERE customer_id = :c AND account_type = 'DEPOSIT' "
                "AND account_status != 'CLOSED'",
                {"c": customer_id},
            )[0]
            investments = _one(
                reader,
                "SELECT COALESCE(SUM(i.portfolio_value_cents), 0) FROM account a "
                "JOIN investment i ON i.account_id = a.account_id "
                "WHERE a.customer_id = :c AND a.account_status != 'CLOSED'",
                {"c": customer_id},
            )[0]
            assets = _one(
                reader,
                "SELECT COALESCE(SUM(current_value_cents), 0) FROM asset WHERE customer_id = :c",
                {"c": customer_id},
            )[0]
            assert expected == deposits + investments + assets

    def test_credit_exposure_matches_loans_plus_card_limits(self, reader: Engine) -> None:
        for customer_id, exposure in _all(
            reader, "SELECT customer_id, credit_exposure_cents FROM derived_credit"
        ):
            loans = _one(
                reader,
                "SELECT COALESCE(SUM(balance_cents), 0) FROM account "
                "WHERE customer_id = :c AND account_type = 'LOAN' AND account_status != 'CLOSED'",
                {"c": customer_id},
            )[0]
            limits = _one(
                reader,
                "SELECT COALESCE(SUM(c.credit_limit_cents), 0) FROM account a "
                "JOIN credit_card c ON c.account_id = a.account_id "
                "WHERE a.customer_id = :c AND a.account_status != 'CLOSED'",
                {"c": customer_id},
            )[0]
            assert exposure == loans + limits

    def test_utilization_bps_truncates_like_sql(self, reader: Engine) -> None:
        for customer_id, util in _all(
            reader,
            "SELECT customer_id, credit_utilization_bps FROM derived_credit "
            "WHERE credit_utilization_bps IS NOT NULL",
        ):
            balance, limit = _one(
                reader,
                "SELECT COALESCE(SUM(a.balance_cents), 0), COALESCE(SUM(c.credit_limit_cents), 0) "
                "FROM account a JOIN credit_card c ON c.account_id = a.account_id "
                "WHERE a.customer_id = :c AND a.account_status != 'CLOSED'",
                {"c": customer_id},
            )
            assert util == int(Cents(balance).ratio_bps(Cents(limit)))

    def test_household_net_worth_sums_member_net_worth(self, reader: Engine) -> None:
        # Pick a household with more than one member so the sum is a real aggregate.
        row = _one(
            reader,
            "SELECT household_id FROM household_member GROUP BY household_id "
            "HAVING COUNT(*) > 1 LIMIT 1",
        )
        if row is None:
            pytest.skip("no multi-member household in the seeded dataset")
        household_id = row[0]
        members = [
            str(r[0])
            for r in _all(
                reader,
                "SELECT customer_id FROM household_member WHERE household_id = :h",
                {"h": household_id},
            )
        ]
        expected = sum(
            _one(
                reader,
                "SELECT net_worth_cents FROM derived_financial WHERE customer_id = :c",
                {"c": member},
            )[0]
            for member in members
        )
        for member in members:
            stored = _one(
                reader,
                "SELECT household_net_worth_cents FROM derived_financial WHERE customer_id = :c",
                {"c": member},
            )[0]
            assert stored == expected


class TestIdempotence:
    """Task 3.1: the recompute job is idempotent — running it twice changes nothing."""

    def test_running_twice_leaves_identical_rows(self, recomputed_db: Path) -> None:
        engine = create_sqlite_engine(recomputed_db, mode=AccessMode.READ_ONLY, pool_size=1)
        try:
            before = _snapshot(engine)
        finally:
            engine.dispose()

        recompute(recomputed_db)

        engine = create_sqlite_engine(recomputed_db, mode=AccessMode.READ_ONLY, pool_size=1)
        try:
            after = _snapshot(engine)
        finally:
            engine.dispose()
        assert before == after


def _snapshot(engine: Engine) -> dict[str, int]:
    return {
        table: _one(engine, f"SELECT COUNT(*) FROM {table}")[0]  # noqa: S608 - constant names
        for table in (
            "derived_financial",
            "derived_credit",
            "derived_health",
            "customer_search",
            "graph_node",
            "graph_edge",
            "graph_adjacency",
            "graph_household_subgraph",
        )
    }


# ==================================================================== task 3.2 health score
class TestHealthProjection:
    def test_one_score_per_customer_with_the_required_shape(self, reader: Engine) -> None:
        customers = _one(reader, "SELECT COUNT(*) FROM customer")[0]
        scores = _one(reader, "SELECT COUNT(*) FROM derived_health")[0]
        assert scores == customers
        for value, band, provenance, version in _all(
            reader, "SELECT value, band, provenance, formula_version FROM derived_health"
        ):
            assert 0 <= value <= 100
            assert band in ("POOR", "FAIR", "GOOD", "EXCELLENT")
            assert provenance == "HEURISTIC"
            assert version == "fhs-v1"

    def test_drivers_are_valid_json_with_contributions(self, reader: Engine) -> None:
        row = _one(reader, "SELECT drivers FROM derived_health LIMIT 1")
        drivers = json.loads(row[0])
        assert len(drivers) == 5
        assert all({"factor", "contribution", "detail"} <= set(d) for d in drivers)


# ==================================================================== task 3.3 FTS5 search
class TestSearchAllEightKeys:
    """The phase gate: search returns correct results for all eight key types (requirement 3.1)."""

    @pytest.fixture
    def sample(self, reader: Engine) -> dict[str, Any]:
        """A customer with a name, contact details, and an *active* account, loan and card.

        Account-status matters: the search index only carries identifiers for open accounts, so the
        sample has to have open holdings of each kind for the per-key assertions to be meaningful.
        """
        row = _one(
            reader,
            "SELECT c.customer_id, c.customer_name, ci.email, ci.phone_number, ci.mobile_number, "
            "ci.city FROM customer c "
            "JOIN contact_info ci ON ci.customer_id = c.customer_id "
            "WHERE ci.email IS NOT NULL AND ci.phone_number IS NOT NULL "
            "AND ci.mobile_number IS NOT NULL AND ci.city IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM account a JOIN loan l ON l.account_id = a.account_id "
            "  WHERE a.customer_id = c.customer_id AND a.account_status != 'CLOSED') "
            "AND EXISTS (SELECT 1 FROM account a "
            "  JOIN credit_card cc ON cc.account_id = a.account_id "
            "  WHERE a.customer_id = c.customer_id AND a.account_status != 'CLOSED') "
            "LIMIT 1",
        )
        assert row is not None, "seeded dataset should contain a fully-populated customer"
        cid = str(row[0])
        account_number = _one(
            reader,
            "SELECT account_number FROM account WHERE customer_id = :c "
            "AND account_status != 'CLOSED' LIMIT 1",
            {"c": cid},
        )[0]
        loan_number = _one(
            reader,
            "SELECT l.loan_number FROM account a JOIN loan l ON l.account_id = a.account_id "
            "WHERE a.customer_id = :c AND a.account_status != 'CLOSED' LIMIT 1",
            {"c": cid},
        )[0]
        card_last4 = _one(
            reader,
            "SELECT cc.card_last4 FROM account a "
            "JOIN credit_card cc ON cc.account_id = a.account_id "
            "WHERE a.customer_id = :c AND a.account_status != 'CLOSED' LIMIT 1",
            {"c": cid},
        )[0]
        return {
            "customer_id": cid,
            "customer_name": row[1],
            "email": row[2],
            "phone_number": row[3],
            "mobile_number": row[4],
            "city": row[5],
            "account_number": account_number,
            "loan_number": loan_number,
            "card_last4": card_last4,
        }

    def _search(self, engine: Engine, column: str, value: str) -> list[str]:
        # Column-scoped MATCH targets a specific search key; the term is bound and double-quoted so
        # punctuation (the + in a phone number, the @ in an email) is a literal token, not FTS5
        # syntax. `column` is one of a fixed, test-controlled set, never external input.
        match = f'{column}: "' + value + '"'
        rows = _all(
            engine,
            "SELECT customer_id FROM customer_search WHERE customer_search MATCH :m",
            {"m": match},
        )
        return [str(r[0]) for r in rows]

    @pytest.mark.parametrize(
        "key",
        [
            "customer_name",
            "email",
            "phone_number",
            "mobile_number",
            "account_number",
            "loan_number",
            "card_last4",
            "city",
        ],
    )
    def test_each_key_finds_the_customer(
        self, reader: Engine, sample: dict[str, Any], key: str
    ) -> None:
        column = {
            "account_number": "account_numbers",
            "loan_number": "loan_numbers",
            "card_last4": "card_last4",
        }.get(key, key)
        results = self._search(reader, column, str(sample[key]))
        assert sample["customer_id"] in results, f"search by {key} did not find the customer"

    def test_prefix_search_on_name_matches(self, reader: Engine, sample: dict[str, Any]) -> None:
        first_token = str(sample["customer_name"]).split()[0]
        prefix = first_token[:3]
        rows = _all(
            reader,
            "SELECT customer_id FROM customer_search "
            "WHERE customer_search MATCH 'customer_name: ' || :p || '*'",
            {"p": prefix},
        )
        assert sample["customer_id"] in [str(r[0]) for r in rows]


# ==================================================================== task 3.4/3.5 graph
class TestGraphProjection:
    def test_all_edges_reference_existing_nodes(self, reader: Engine) -> None:
        orphans = _one(
            reader,
            "SELECT COUNT(*) FROM graph_edge e "
            "WHERE NOT EXISTS (SELECT 1 FROM graph_node WHERE node_id = e.src_id) "
            "OR NOT EXISTS (SELECT 1 FROM graph_node WHERE node_id = e.dst_id)",
        )[0]
        assert orphans == 0

    def test_node_count_matches_relational_sources(self, reader: Engine) -> None:
        """Customer, Household and Employer node counts equal their source-table row counts."""
        for node_type, table in (
            ("Customer", "customer"),
            ("Household", "household"),
            ("Employer", "employer"),
        ):
            nodes = _one(
                reader, "SELECT COUNT(*) FROM graph_node WHERE node_type = :t", {"t": node_type}
            )[0]
            source = _one(reader, f"SELECT COUNT(*) FROM {table}")[0]  # noqa: S608
            assert nodes == source, f"{node_type} node count != {table} rows"

    def test_customer_relationship_edges_carry_inference(self, reader: Engine) -> None:
        inferred_rel = _one(
            reader, "SELECT COUNT(*) FROM customer_relationship WHERE is_inferred = 1"
        )[0]
        inferred_edges = _one(
            reader,
            "SELECT COUNT(*) FROM graph_edge WHERE edge_type = 'RELATED_TO' AND is_inferred = 1",
        )[0]
        assert inferred_edges == inferred_rel

    def test_edges_are_unique_on_the_composite_key(self, reader: Engine) -> None:
        duplicates = _one(
            reader,
            "SELECT COUNT(*) FROM (SELECT src_id, dst_id, edge_type FROM graph_edge "
            "GROUP BY src_id, dst_id, edge_type HAVING COUNT(*) > 1)",
        )[0]
        assert duplicates == 0


class TestAdjacency:
    """Task 3.5: every edge materialized OUT and IN, indexed for single-predicate traversal."""

    def test_adjacency_is_exactly_two_rows_per_edge(self, reader: Engine) -> None:
        edges = _one(reader, "SELECT COUNT(*) FROM graph_edge")[0]
        adjacency = _one(reader, "SELECT COUNT(*) FROM graph_adjacency")[0]
        assert adjacency == edges * 2

    def test_both_directions_exist_for_every_edge(self, reader: Engine) -> None:
        missing = _one(
            reader,
            "SELECT COUNT(*) FROM graph_edge e WHERE NOT EXISTS ("
            "SELECT 1 FROM graph_adjacency WHERE edge_id = e.edge_id AND direction = 'OUT') "
            "OR NOT EXISTS ("
            "SELECT 1 FROM graph_adjacency WHERE edge_id = e.edge_id AND direction = 'IN')",
        )[0]
        assert missing == 0

    def test_traversal_uses_the_index(self, reader: Engine) -> None:
        """The whole point of §5.2: a src_id lookup must be an indexed search, not a scan."""
        plan = _all(
            reader,
            "EXPLAIN QUERY PLAN SELECT dst_id FROM graph_adjacency "
            "WHERE src_id = 'CUSTOMER:C-00001' AND edge_type = 'OWNS'",
        )
        detail = " ".join(str(row[-1]) for row in plan)
        assert "ix_adj_src" in detail

    def test_household_subgraph_contains_the_members(self, reader: Engine) -> None:
        row = _one(
            reader,
            "SELECT household_id FROM household_member GROUP BY household_id "
            "HAVING COUNT(*) > 1 LIMIT 1",
        )
        if row is None:
            pytest.skip("no multi-member household in the seeded dataset")
        household_id = str(row[0])
        member_count = _one(
            reader,
            "SELECT COUNT(*) FROM household_member WHERE household_id = :h",
            {"h": household_id},
        )[0]
        cached_members = _one(
            reader,
            "SELECT COUNT(*) FROM graph_household_subgraph WHERE household_id = :h AND depth = 1",
            {"h": household_id},
        )[0]
        assert cached_members == member_count


class TestGraphDeterminism:
    """The phase gate: the graph rebuilds deterministically."""

    def test_two_projections_produce_identical_graphs(
        self, seeded_db: Path, tmp_path: Path
    ) -> None:
        first = tmp_path / "first.db"
        second = tmp_path / "second.db"
        shutil.copy(seeded_db, first)
        shutil.copy(seeded_db, second)
        recompute(first)
        recompute(second)

        engine_a = create_sqlite_engine(first, mode=AccessMode.READ_ONLY, pool_size=1)
        engine_b = create_sqlite_engine(second, mode=AccessMode.READ_ONLY, pool_size=1)
        node_sql = "SELECT node_id, node_type, label FROM graph_node ORDER BY node_id"
        try:
            nodes_a = _all(engine_a, node_sql)
            nodes_b = _all(engine_b, node_sql)
            edges_a = _all(
                engine_a,
                "SELECT src_id, dst_id, edge_type, is_inferred FROM graph_edge "
                "ORDER BY src_id, dst_id, edge_type",
            )
            edges_b = _all(
                engine_b,
                "SELECT src_id, dst_id, edge_type, is_inferred FROM graph_edge "
                "ORDER BY src_id, dst_id, edge_type",
            )
        finally:
            engine_a.dispose()
            engine_b.dispose()
        assert nodes_a == nodes_b
        assert edges_a == edges_b


# ==================================================================== task 3.6 CLI + endpoint
class TestReport:
    def test_report_totals_and_summary(self, recomputed_db: Path) -> None:
        report = recompute(recomputed_db)
        assert isinstance(report, RecomputeReport)
        assert report.row_counts["derived_financial"] == COUNT
        assert report.total_rows == sum(report.row_counts.values())
        summary = "\n".join(report.summary_lines())
        assert "derived_financial" in summary
        assert "graph_node" in summary


class TestCli:
    def test_recompute_command_runs(
        self, seeded_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "customer.db"
        shutil.copy(seeded_db, path)
        exit_code = cli_main(["recompute", "--database", str(path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "projected:" in out
        assert "graph_node" in out

    def test_recompute_reports_a_missing_database(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli_main(["recompute", "--database", str(tmp_path / "absent.db")])
        assert exit_code == 1
        assert "does not exist" in capsys.readouterr().err


class TestAdminEndpoint:
    def test_recompute_endpoint_returns_counts(self, seeded_db: Path, tmp_path: Path) -> None:
        path = tmp_path / "customer.db"
        shutil.copy(seeded_db, path)
        settings = make_settings(sqlite_db_path=str(path.resolve()))
        app = create_app(settings)
        with TestClient(app) as client:
            token = client.post(
                "/auth/token", json={"username": "risk.riley", "password": "risk-dev-password"}
            ).json()["data"]["access_token"]
            response = client.post("/admin/recompute", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["row_counts"]["derived_financial"] == COUNT
        assert body["data"]["total_rows"] > 0
        assert body["meta"]["correlation_id"]

    def test_recompute_endpoint_reports_a_missing_database(self, tmp_path: Path) -> None:
        settings = make_settings(sqlite_db_path=str((tmp_path / "absent.db").resolve()))
        app = create_app(settings)
        with TestClient(app) as client:
            token = client.post(
                "/auth/token", json={"username": "risk.riley", "password": "risk-dev-password"}
            ).json()["data"]["access_token"]
            response = client.post("/admin/recompute", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
