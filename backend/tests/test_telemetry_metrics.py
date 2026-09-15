"""Tests for the Phase 10 metric layer: label allowlist, cost estimator and the inventory.

The metric-label allowlist is the metrics-side counterpart of the span-attribute leakage test
(design §13.4): a metric with a ``customer_id`` label is both a cardinality incident and a privacy
incident, so it gets the same disproportionate weight here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from c360.core.telemetry import metrics as metrics_module
from c360.core.telemetry.cost import CostEstimator, ModelPrice
from c360.core.telemetry.labels import (
    ALLOWED_METRIC_LABELS,
    label_permitted,
    rejected_labels,
    scrub_labels,
)
from c360.core.telemetry.metrics import PlatformMetrics, reset_metrics, setup_metrics

# ---------------------------------------------------------------- label allowlist (task 10.3)

#: The keys design §13.4 names as forbidden or unbounded. None may ever be a metric label.
_FORBIDDEN_LABELS = [
    "customer_id",
    "customer_hash",
    "balance_cents",
    "net_worth_cents",
    "account_number",
    "email",
    "query",
    "prompt",
    "completion",
    "question",
    "answer",
]


class TestMetricLabelAllowlist:
    def test_permits_the_seven_design_dimensions(self) -> None:
        for key in ("role", "segment", "route", "agent", "model", "outcome", "domain"):
            assert label_permitted(key), key

    @pytest.mark.parametrize("key", _FORBIDDEN_LABELS)
    def test_refuses_identifying_and_monetary_labels(self, key: str) -> None:
        assert not label_permitted(key)
        assert key not in ALLOWED_METRIC_LABELS

    def test_scrub_drops_forbidden_and_keeps_allowed(self) -> None:
        scrubbed = scrub_labels(
            {"role": "RM", "customer_id": "C-1", "balance_cents": 500, "route": "/x"}
        )
        assert scrubbed == {"role": "RM", "route": "/x"}

    def test_scrub_coerces_values_to_strings(self) -> None:
        scrubbed = scrub_labels({"http.response.status_code": 200})
        assert scrubbed == {"http.response.status_code": "200"}

    def test_rejected_labels_reports_what_scrub_drops(self) -> None:
        labels = {"role": "RM", "customer_id": "C-1", "balance_cents": 500}
        assert set(rejected_labels(labels)) == {"customer_id", "balance_cents"}

    def test_empty_mapping_is_empty(self) -> None:
        assert scrub_labels(None) == {}
        assert rejected_labels(None) == ()

    def test_no_forbidden_label_is_in_the_allowlist(self) -> None:
        # The bright line, asserted directly: the allowlist and the forbidden set are disjoint.
        assert ALLOWED_METRIC_LABELS.isdisjoint(_FORBIDDEN_LABELS)


# ---------------------------------------------------------------- cost estimator (task 10.2)


class TestCostEstimator:
    def test_computes_integer_micro_usd_from_tokens(self) -> None:
        estimator = CostEstimator({"m": ModelPrice(3000, 15000)})
        # 1000 input @ 3000/1k = 3000; 500 output @ 15000/1k = 7500.
        assert estimator.micro_usd("m", input_tokens=1000, output_tokens=500) == 3000 + 7500

    def test_integer_arithmetic_floors_sub_unit_costs(self) -> None:
        estimator = CostEstimator({"m": ModelPrice(20, 0)})
        # 10 tokens @ 20/1k = 0.2 micro-USD -> floors to 0 (no float ever enters the ledger).
        assert estimator.micro_usd("m", input_tokens=10, output_tokens=0) == 0

    def test_unknown_model_costs_zero_and_does_not_raise(self) -> None:
        estimator = CostEstimator({})
        assert estimator.micro_usd("nope", input_tokens=1000, output_tokens=1000) == 0

    def test_loads_from_the_shipped_price_file(self, tmp_path: Path) -> None:
        path = tmp_path / "prices.json"
        path.write_text(
            json.dumps({"models": {"m": {"input": 1000, "output": 2000}}}), encoding="utf-8"
        )
        estimator = CostEstimator.from_file(path)
        assert estimator.micro_usd("m", input_tokens=1000, output_tokens=1000) == 3000

    def test_missing_file_yields_empty_table(self, tmp_path: Path) -> None:
        estimator = CostEstimator.from_file(tmp_path / "absent.json")
        assert estimator.micro_usd("m", input_tokens=1000, output_tokens=0) == 0

    def test_repository_price_file_parses(self) -> None:
        # The real shipped table must load without error and price a known model.
        root = Path(__file__).resolve().parents[2]
        estimator = CostEstimator.from_file(root / "config" / "bedrock_prices.json")
        cost = estimator.micro_usd(
            "anthropic.claude-3-5-sonnet-20241022-v2:0", input_tokens=1000, output_tokens=1000
        )
        assert cost == 3000 + 15000


# ---------------------------------------------------------------- the inventory (task 10.2)


@pytest.fixture
def installed_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[PlatformMetrics, InMemoryMetricReader]]:
    """Install a PlatformMetrics whose meter feeds an in-memory reader, then tear it down."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics_module, "get_meter", provider.get_meter)
    reset_metrics()
    platform = setup_metrics(CostEstimator({"m": ModelPrice(1000, 2000)}))
    yield platform, reader
    reset_metrics()


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    names: set[str] = set()
    if data is None:
        return names
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                names.add(metric.name)
    return names


class TestMetricInventory:
    def test_records_http_red(
        self, installed_metrics: tuple[PlatformMetrics, InMemoryMetricReader]
    ) -> None:
        platform, reader = installed_metrics
        platform.record_http(
            method="GET", route="/x", status_code=500, duration_ms=12.0, over_budget=True
        )
        names = _metric_names(reader)
        assert "c360.http.server.request.duration" in names
        assert "c360.http.server.requests" in names
        assert "c360.http.server.errors" in names
        assert "c360.http.server.budget_breach" in names

    def test_records_agent_and_model_with_cost(
        self, installed_metrics: tuple[PlatformMetrics, InMemoryMetricReader]
    ) -> None:
        platform, reader = installed_metrics
        platform.record_agent(agent="risk", outcome="success", duration_ms=100.0, citations=3)
        platform.record_model_call(
            model="m", agent="risk", duration_ms=90.0, input_tokens=1000, output_tokens=1000
        )
        names = _metric_names(reader)
        assert "c360.agent.duration" in names
        assert "c360.agent.outcome" in names
        assert "gen_ai.client.token.usage" in names
        assert "c360.model.estimated_cost.micro_usd" in names

    def test_records_security_and_graph(
        self, installed_metrics: tuple[PlatformMetrics, InMemoryMetricReader]
    ) -> None:
        platform, reader = installed_metrics
        platform.record_denied_access(role="MARKETING")
        platform.record_masking(field_group="BALANCES")
        platform.record_breaker_transition(breaker="generation", state="open")
        platform.record_graph(hops=3, nodes_visited=42, truncated=True)
        names = _metric_names(reader)
        assert "c360.security.denied_access" in names
        assert "c360.security.masking_applied" in names
        assert "c360.security.breaker_transition" in names
        assert "c360.graph.truncated" in names

    def test_no_forbidden_label_reaches_an_exported_metric(
        self, installed_metrics: tuple[PlatformMetrics, InMemoryMetricReader]
    ) -> None:
        # The telemetry-leakage assertion for metrics (task 10.3): drive every labelled instrument
        # through the facade and assert no exported data point carries a forbidden label key. The
        # facade scrubs labels internally, so even a caller that supplied customer_id cannot leak
        # it — but here we assert on the *exported* data, which is what a backend would receive.
        platform, reader = installed_metrics
        platform.record_http(
            method="GET",
            route="/customers/{id}",
            status_code=200,
            duration_ms=5.0,
            over_budget=False,
        )
        platform.record_agent(agent="risk", outcome="success", duration_ms=1.0, citations=2)
        platform.record_model_call(
            model="m", agent="risk", duration_ms=1.0, input_tokens=10, output_tokens=10
        )
        platform.record_denied_access(role="RM")
        platform.record_masking(field_group="BALANCES")
        platform.record_qa(tool_calls=2, refused_reason="no_guidance", clarified=True)

        data = reader.get_metrics_data()
        assert data is not None
        seen_label_keys: set[str] = set()
        for resource_metric in data.resource_metrics:
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    for point in metric.data.data_points:
                        seen_label_keys.update((point.attributes or {}).keys())
        assert seen_label_keys.isdisjoint(_FORBIDDEN_LABELS)
        # And every label that did appear is on the allowlist — no leakage by any name.
        assert seen_label_keys <= ALLOWED_METRIC_LABELS

    def test_pre_startup_calls_are_silent(self) -> None:
        # Before ensure(), every record method is a no-op rather than an attribute error.
        reset_metrics()
        platform = PlatformMetrics(CostEstimator({}))
        platform.record_http(
            method="GET", route="/x", status_code=200, duration_ms=1.0, over_budget=False
        )
        platform.record_agent(agent="a", outcome="success", duration_ms=1.0, citations=0)
        # No exception is the assertion.
