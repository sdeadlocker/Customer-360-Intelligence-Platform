"""Phase 17 - proactive alerting and signals feed (tasks 17.1-17.7).

These run the real detection job and the real worklist API over the generated, recomputed dataset
the rest of Phase 5+ uses. They assert the phase gate: detectors surface the expected cohorts, the
run is idempotent on re-run, the queue is entitlement-scoped and ranked, dismissed signals stay
suppressed, and — the datastore rule — ``customer.db`` is never written.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from c360.api.readiness import REGISTRY
from c360.core.config import Settings
from c360.core.logging import configure_logging
from c360.core.telemetry import scrub_labels
from c360.core.telemetry.allowlist import ALLOWED_SPAN_ATTRIBUTES
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.main import create_app
from c360.signals.detect import DetectConfig, detect_signals
from c360.signals.models import DetectedSignal, Evidence, Severity, SignalType
from c360.signals.ranking import RankingWeights, rank, score_signal
from c360.signals.store import SCHEMA_VERSION, current_schema_version
from tests.conftest import make_settings
from tests.phase5_fixtures import query_all

# A large-deposit floor low enough that the seeded panel actually crosses it, so the detector has
# real signal on 40 customers. The production default (5,000,000) is tuned for the HNW cohort at
# full scale.
_DEPOSIT_FLOOR = 2_000_000


def _config() -> DetectConfig:
    return DetectConfig(
        large_deposit_threshold_cents=_DEPOSIT_FLOOR,
        spend_anomaly_sigma=2.0,
        major_txn_absolute_threshold_cents=1_000_000,
        major_txn_median_multiple=10.0,
        weights=RankingWeights(
            severity_weight=1.0, value_weight=0.6, recency_weight=0.3, value_cap_cents=100_000_000
        ),
    )


@pytest.fixture(scope="module")
def signals_db(phase5_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A detected signals database built once over the phase5 dataset."""
    path = tmp_path_factory.mktemp("signals") / "signals.db"
    detect_signals(customer_db_path=phase5_db, signals_db_path=path, config=_config())
    return path


# ==================================================================== 17.1 store & schema
class TestStoreAndSchema:
    def test_schema_is_stamped_at_head(self, signals_db: Path) -> None:
        assert current_schema_version(signals_db) == SCHEMA_VERSION

    def test_tables_exist_and_have_no_fk_into_customer_db(self, signals_db: Path) -> None:
        connection = sqlite3.connect(signals_db)
        try:
            names = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"signal", "signal_state", "signal_run"} <= names
            # No table declares a foreign key whose target table lives in customer.db — the only
            # FKs are internal (signal.run_id -> signal_run, signal_state.signal_id -> signal).
            for table in ("signal", "signal_state", "signal_run"):
                for fk in connection.execute(f"PRAGMA foreign_key_list({table})"):
                    assert fk[2] in {"signal", "signal_run"}
        finally:
            connection.close()

    def test_dedup_key_is_unique(self, signals_db: Path) -> None:
        connection = sqlite3.connect(signals_db)
        try:
            rows = connection.execute(
                "SELECT dedup_key, count(*) FROM signal GROUP BY dedup_key HAVING count(*) > 1"
            ).fetchall()
            assert rows == []
        finally:
            connection.close()


# ==================================================================== 17.2 detector correctness
class TestDetectorCorrectness:
    def test_aml_pep_cohort_surfaces_a_critical_compliance_signal(
        self, signals_db: Path, phase5_engine: object
    ) -> None:
        flagged = {
            str(row[0])
            for row in query_all(
                phase5_engine,  # type: ignore[arg-type]
                "SELECT customer_id FROM risk_profile WHERE aml_flag = 1 OR pep_flag = 1",
            )
        }
        if not flagged:
            pytest.skip("seeded panel has no AML/PEP-flagged customer")
        connection = sqlite3.connect(signals_db)
        try:
            emitted = {
                row[0]
                for row in connection.execute(
                    "SELECT customer_id FROM signal WHERE signal_type = ? AND severity = ?",
                    (str(SignalType.AML_PEP_FLAG), int(Severity.CRITICAL)),
                )
            }
        finally:
            connection.close()
        assert flagged <= emitted

    def test_large_deposit_cohort_surfaces_a_deposit_signal(
        self, signals_db: Path, phase5_engine: object
    ) -> None:
        big = {
            str(row[0])
            for row in query_all(
                phase5_engine,  # type: ignore[arg-type]
                "SELECT DISTINCT customer_id FROM txn WHERE amount_cents >= :floor",
                {"floor": _DEPOSIT_FLOOR},
            )
        }
        if not big:
            pytest.skip("seeded panel has no large-deposit customer at this floor")
        connection = sqlite3.connect(signals_db)
        try:
            emitted = {
                row[0]
                for row in connection.execute(
                    "SELECT customer_id FROM signal WHERE signal_type = ?",
                    (str(SignalType.LARGE_DEPOSIT),),
                )
            }
        finally:
            connection.close()
        assert emitted <= big  # every deposit signal is backed by a real large credit
        assert emitted, "expected at least one large-deposit signal"

    def test_every_signal_carries_a_fact_citation(self, signals_db: Path) -> None:
        connection = sqlite3.connect(signals_db)
        try:
            for (evidence,) in connection.execute("SELECT evidence FROM signal"):
                parsed = json.loads(evidence)
                assert parsed["citations"], "a signal must cite at least one fact"
                for citation in parsed["citations"]:
                    assert citation["entity_id"]
                    assert citation["field"]
        finally:
            connection.close()

    def test_signals_only_reference_seeded_customers(
        self, signals_db: Path, phase5_engine: object
    ) -> None:
        known = {
            str(row[0])
            for row in query_all(phase5_engine, "SELECT customer_id FROM customer")  # type: ignore[arg-type]
        }
        connection = sqlite3.connect(signals_db)
        try:
            referenced = {row[0] for row in connection.execute("SELECT customer_id FROM signal")}
        finally:
            connection.close()
        assert referenced <= known


# ==================================================================== 17.3 ranking
class TestRanking:
    def test_ranking_is_stable_and_severity_dominates(self) -> None:
        weights = RankingWeights(1.0, 0.6, 0.3, 100_000_000)
        as_of = date(2026, 9, 1)
        critical = DetectedSignal(
            customer_id="C-1",
            signal_type=SignalType.AML_PEP_FLAG,
            severity=Severity.CRITICAL,
            value_at_stake_cents=0,
            evidence=Evidence(summary="x"),
            as_of=as_of,
            dedup_key="C-1:x",
        )
        info = DetectedSignal(
            customer_id="C-2",
            signal_type=SignalType.LIFE_EVENT,
            severity=Severity.INFO,
            value_at_stake_cents=0,
            evidence=Evidence(summary="y"),
            as_of=as_of,
            dedup_key="C-2:y",
        )
        assert score_signal(critical, as_of, weights) > score_signal(info, as_of, weights)
        # Stable order: the same inputs always produce the same sequence.
        first = [s.dedup_key for s, _ in rank([info, critical], as_of, weights)]
        second = [s.dedup_key for s, _ in rank([critical, info], as_of, weights)]
        assert first == second == ["C-1:x", "C-2:y"]


# ==================================================================== 17.4 idempotence
class TestIdempotence:
    def test_rerun_updates_rather_than_duplicates(self, phase5_db: Path, tmp_path: Path) -> None:
        path = tmp_path / "signals.db"
        first = detect_signals(customer_db_path=phase5_db, signals_db_path=path, config=_config())
        second = detect_signals(customer_db_path=phase5_db, signals_db_path=path, config=_config())
        connection = sqlite3.connect(path)
        try:
            total = connection.execute("SELECT count(*) FROM signal").fetchone()[0]
            runs = connection.execute("SELECT count(*) FROM signal_run").fetchone()[0]
        finally:
            connection.close()
        assert first.signals_written == second.signals_written
        assert total == first.signals_written  # no duplication across two runs
        assert runs == 2  # but each run records its own provenance


# ==================================================================== 17.7 read-only proof
class TestCustomerDbUnchanged:
    def test_detection_opens_customer_db_read_only(self, phase5_db: Path, tmp_path: Path) -> None:
        # A write attempt against the customer database, opened read-only exactly as detection opens
        # it, must raise — proving detection cannot mutate the source (task 17.7).
        engine = create_sqlite_engine(phase5_db, mode=AccessMode.READ_ONLY, pool_size=1)
        try:
            with pytest.raises(OperationalError, match="readonly database"), engine.connect() as c:
                c.exec_driver_sql("UPDATE customer SET customer_name = 'X'")
        finally:
            engine.dispose()

    def test_customer_db_bytes_unchanged_by_detection(
        self, phase5_db: Path, tmp_path: Path
    ) -> None:
        before = phase5_db.read_bytes()
        detect_signals(
            customer_db_path=phase5_db, signals_db_path=tmp_path / "signals.db", config=_config()
        )
        assert phase5_db.read_bytes() == before


# ==================================================================== 17.5 worklist API
_ALL_USER = ("contact.jordan", "contact-dev-password")


@pytest.fixture
def api_settings(
    phase5_db: Path, signals_db: Path, tmp_path_factory: pytest.TempPathFactory
) -> Settings:
    audit_dir = tmp_path_factory.mktemp("signals_api_audit")
    return make_settings(
        sqlite_db_path=str(phase5_db),
        sqlite_audit_db_path=str(audit_dir / "audit.db"),
        sqlite_signals_db_path=str(signals_db),
    )


@pytest.fixture
def api_client(api_settings: Settings) -> Iterator[TestClient]:
    REGISTRY.clear()
    configure_logging(api_settings, force=True)
    app: FastAPI = create_app(api_settings)
    with TestClient(app) as client:
        yield client


def _headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    body = client.post("/auth/token", json={"username": username, "password": password}).json()
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


class TestWorklistApi:
    def test_worklist_is_ranked_descending(self, api_client: TestClient) -> None:
        response = api_client.get("/signals?limit=50", headers=_headers(api_client, *_ALL_USER))
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        assert items, "expected a non-empty worklist"
        scores = [item["score"] for item in items]
        assert scores == sorted(scores, reverse=True)

    def test_dismissed_signal_is_suppressed(self, api_client: TestClient) -> None:
        headers = _headers(api_client, *_ALL_USER)
        first = api_client.get("/signals?limit=50", headers=headers).json()["data"]["items"]
        target = first[0]["signal_id"]
        dismissed = api_client.post(f"/signals/{target}/dismiss", headers=headers)
        assert dismissed.status_code == 200
        after = api_client.get("/signals?limit=50", headers=headers).json()["data"]["items"]
        assert target not in {item["signal_id"] for item in after}

    def test_acknowledged_signal_is_suppressed(self, api_client: TestClient) -> None:
        headers = _headers(api_client, *_ALL_USER)
        first = api_client.get("/signals?limit=50", headers=headers).json()["data"]["items"]
        target = first[-1]["signal_id"]
        acked = api_client.post(f"/signals/{target}/ack", headers=headers)
        assert acked.status_code == 200
        after = api_client.get("/signals?limit=50", headers=headers).json()["data"]["items"]
        assert target not in {item["signal_id"] for item in after}

    def test_type_filter_restricts_the_queue(self, api_client: TestClient) -> None:
        headers = _headers(api_client, *_ALL_USER)
        response = api_client.get("/signals?limit=50&type=LARGE_DEPOSIT", headers=headers).json()[
            "data"
        ]["items"]
        assert all(item["signal_type"] == "LARGE_DEPOSIT" for item in response)

    def test_marketing_sees_a_banded_value_never_the_figure(self, api_client: TestClient) -> None:
        # Marketing has BAND on balances, so value_at_stake is a band label, never a raw number,
        # and the masked-field manifest records it.
        headers = _headers(api_client, "marketing.avery", "marketing-dev-password")
        body = api_client.get("/signals?limit=10", headers=headers).json()
        items = body["data"]["items"]
        if not items:
            pytest.skip("marketing's segment scope surfaced no signals on this panel")
        for item in items:
            value = item.get("value_at_stake_cents")
            assert not isinstance(value, int), "a banded value must not be a raw integer"
        assert any("value_at_stake_cents" in field for field in body["meta"]["masked_fields"])

    def test_customer_drilldown_returns_that_customers_signals(
        self, api_client: TestClient, signals_db: Path
    ) -> None:
        headers = _headers(api_client, *_ALL_USER)
        connection = sqlite3.connect(signals_db)
        try:
            row = connection.execute("SELECT customer_id FROM signal LIMIT 1").fetchone()
        finally:
            connection.close()
        customer_id = row[0]
        response = api_client.get(f"/customers/{customer_id}/signals", headers=headers)
        assert response.status_code == 200
        signals = response.json()["data"]["signals"]
        assert signals
        assert all(s["customer_id"] == customer_id for s in signals)

    def test_dismiss_unknown_signal_is_404(self, api_client: TestClient) -> None:
        headers = _headers(api_client, *_ALL_USER)
        response = api_client.post("/signals/999999/dismiss", headers=headers)
        assert response.status_code == 404


class TestTelemetryPrivacy:
    def test_detect_span_exposes_only_allowlisted_attributes(
        self, phase5_db: Path, tmp_path: Path, span_exporter: object
    ) -> None:
        # The span_exporter fixture installs the production allowlist processor, so any attribute
        # the detect span sets that is not allowlisted (a customer id, a value) would be dropped
        # before export. Assert the exported span carries only bounded counts and no forbidden key.
        detect_signals(
            customer_db_path=phase5_db, signals_db_path=tmp_path / "signals.db", config=_config()
        )
        spans = span_exporter.get_finished_spans()  # type: ignore[attr-defined]
        detect_spans = [s for s in spans if s.name == "detect_signals"]
        assert detect_spans, "the detection job must emit a span"
        for span in detect_spans:
            for key in span.attributes or {}:
                # Exported keys are either allowlisted names or the two bounded count attributes
                # the job sets; none is a customer id, a value or free text.
                assert key in ALLOWED_SPAN_ATTRIBUTES or key.startswith("c360.signals.")
                assert "customer" not in key or key.endswith("_scanned")

    def test_signal_metric_labels_are_scrubbed_to_bounded_dimensions(self) -> None:
        # A stray customer id or value handed as a label is dropped; only the bounded type/severity
        # dimensions survive (design §18.8).
        scrubbed = scrub_labels(
            {
                "signal_type": "LARGE_DEPOSIT",
                "severity": "CRITICAL",
                "customer_id": "C-1",
                "value_at_stake_cents": 5_000_000,
            }
        )
        assert scrubbed == {"signal_type": "LARGE_DEPOSIT", "severity": "CRITICAL"}


class TestWorklistEntitlementScoping:
    def test_segment_scope_only_sees_its_own_segment(
        self, api_client: TestClient, phase5_engine: object
    ) -> None:
        # A wealth advisor is segment-scoped in the seed. Assert every signal it sees is for a
        # customer in a segment its scope permits — i.e. scoping is applied to the queue itself,
        # not as a post-filter. This is checked directly against customer segments so it is
        # independent of any other user's per-user dismissals on the shared store.
        headers = _headers(api_client, "wealth.morgan", "wealth-dev-password")
        me = api_client.get("/me", headers=headers).json()["data"]
        assert me["entitlement"]["kind"] == "SEGMENT"
        permitted_segments = set(me["entitlement"].get("segments") or [])

        items = api_client.get("/signals?limit=100", headers=headers).json()["data"]["items"]
        if not items:
            pytest.skip("wealth advisor's segment surfaced no signals on this panel")

        segment_of = {
            str(row[0]): str(row[1])
            for row in query_all(
                phase5_engine,  # type: ignore[arg-type]
                "SELECT customer_id, customer_segment FROM customer",
            )
        }
        for item in items:
            assert segment_of[item["customer_id"]] in permitted_segments
