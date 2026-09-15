"""Phase 0 gate: the app starts, `/health` returns 200, and every response is traceable."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from c360.api.envelope import ApiError, ErrorCode
from c360.api.readiness import REGISTRY, CheckResult, CheckStatus
from c360.core.context import CORRELATION_ID_HEADER
from c360.core.telemetry import AllowlistSpanProcessor, SpanAttr, get_telemetry


class TestHealth:
    def test_health_returns_200_with_the_standard_envelope(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["status"] == "ok"
        assert body["data"]["version"]
        assert body["meta"]["correlation_id"]
        assert body["meta"]["as_of"]
        assert body["meta"]["masked_fields"] == []
        assert body["meta"]["errors"] == []

    def test_health_carries_a_trace_id(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["meta"]["trace_id"] is not None
        assert len(body["meta"]["trace_id"]) == 32


class TestCorrelationId:
    def test_an_id_is_generated_at_the_edge(self, client: TestClient) -> None:
        response = client.get("/health")
        header = response.headers[CORRELATION_ID_HEADER]
        assert len(header) == 32
        assert response.json()["meta"]["correlation_id"] == header

    def test_a_caller_supplied_id_is_adopted(self, client: TestClient) -> None:
        supplied = "0123456789abcdef0123456789abcdef"
        response = client.get("/health", headers={CORRELATION_ID_HEADER: supplied})
        assert response.headers[CORRELATION_ID_HEADER] == supplied
        assert response.json()["meta"]["correlation_id"] == supplied

    def test_a_malformed_id_is_replaced_rather_than_echoed(self, client: TestClient) -> None:
        """An inbound header reaches logs, spans and audit records: it is untrusted input."""
        hostile = "<script>alert(1)</script>"
        response = client.get("/health", headers={CORRELATION_ID_HEADER: hostile})
        assert response.headers[CORRELATION_ID_HEADER] != hostile
        assert len(response.headers[CORRELATION_ID_HEADER]) == 32

    def test_ids_differ_between_requests(self, client: TestClient) -> None:
        first = client.get("/health").headers[CORRELATION_ID_HEADER]
        second = client.get("/health").headers[CORRELATION_ID_HEADER]
        assert first != second

    def test_trace_id_header_is_returned(self, client: TestClient) -> None:
        assert len(client.get("/health").headers["X-Trace-Id"]) == 32


class TestReadiness:
    def test_ready_reports_registered_components(self, client: TestClient) -> None:
        response = client.get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["status"] == "ready"
        # Phase 4 adds the audit-writer liveness check alongside the Phase 0 config check; Phase 7
        # adds the knowledge-base check; Phase 10 (task 10.8) adds the customer-database integrity
        # and schema check and the model-provider reachability check; Phase 18 (task 18.7) adds the
        # report-store schema check. The knowledge, customer_db and reports checks are SKIPPED
        # here — no knowledge.db, customer.db or reports.db is built in this test.
        assert [component["name"] for component in body["data"]["components"]] == [
            "config",
            "audit",
            "knowledge",
            "customer_db",
            "model",
            "reports",
        ]
        components = {c["name"]: c for c in body["data"]["components"]}
        # The mock provider is always reachable, so the model check passes and names the provider.
        assert components["model"]["status"] == "pass"
        assert "model=" in components["model"]["detail"]

    def test_a_failing_component_fails_readiness_closed(self, client: TestClient) -> None:
        REGISTRY.register(
            "customer_db",
            lambda: CheckResult(name="customer_db", status=CheckStatus.FAIL, detail="missing"),
        )
        try:
            response = client.get("/ready")
            assert response.status_code == 503
            assert response.json()["data"]["status"] == "not_ready"
        finally:
            REGISTRY.unregister("customer_db")

    def test_a_skipped_component_does_not_block(self, client: TestClient) -> None:
        REGISTRY.register(
            "knowledge_db",
            lambda: CheckResult(name="knowledge_db", status=CheckStatus.SKIPPED),
        )
        try:
            assert client.get("/ready").status_code == 200
        finally:
            REGISTRY.unregister("knowledge_db")

    def test_a_raising_component_is_a_failing_component(self, client: TestClient) -> None:
        def _explode() -> CheckResult:
            raise RuntimeError("connection refused to 10.0.0.5")

        REGISTRY.register("audit_writer", _explode)
        try:
            response = client.get("/ready")
            assert response.status_code == 503
            detail = json.dumps(response.json())
            assert "10.0.0.5" not in detail, "check detail must not echo internals"
        finally:
            REGISTRY.unregister("audit_writer")


def _auth_header(client: TestClient) -> dict[str, str]:
    """A bearer header for a seeded user. Phase 4 put every non-public route behind authentication,
    so the error-envelope tests must authenticate to reach the handler they are exercising."""
    token = client.post(
        "/auth/token", json={"username": "risk.riley", "password": "risk-dev-password"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestErrorEnvelope:
    def test_unknown_route_returns_the_error_envelope(self, client: TestClient) -> None:
        response = client.get("/does-not-exist", headers=_auth_header(client))
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == ErrorCode.CUSTOMER_NOT_FOUND
        assert error["correlation_id"]

    def test_unauthenticated_unknown_route_is_401_not_404(self, client: TestClient) -> None:
        """An unauthenticated caller learns nothing about which routes exist (requirement 12.1)."""
        assert client.get("/does-not-exist").status_code == 401

    def test_validation_errors_name_fields_without_echoing_values(self, app: FastAPI) -> None:
        @app.get("/_test/echo")
        async def _echo(count: int) -> dict[str, int]:  # pragma: no cover - exercised via HTTP
            return {"count": count}

        with TestClient(app) as client:
            response = client.get(
                "/_test/echo",
                params={"count": "4111111111111111x"},
                headers=_auth_header(client),
            )

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == ErrorCode.VALIDATION_ERROR
        assert "count" in error["message"]
        assert "4111111111111111" not in error["message"]

    def test_application_errors_map_to_documented_codes(self, app: FastAPI) -> None:
        @app.get("/_test/denied")
        async def _denied() -> None:  # pragma: no cover - exercised via HTTP
            raise ApiError(ErrorCode.ENTITLEMENT_DENIED, "Not entitled to this customer.")

        with TestClient(app) as client:
            response = client.get("/_test/denied", headers=_auth_header(client))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.ENTITLEMENT_DENIED

    def test_unexpected_errors_are_not_leaked_to_the_client(self, app: FastAPI) -> None:
        @app.get("/_test/boom")
        async def _boom() -> None:  # pragma: no cover - exercised via HTTP
            raise RuntimeError("net worth is $1,250,000.00 for a@b.com")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/_test/boom", headers=_auth_header(client))

        assert response.status_code == 500
        body = response.text
        assert "1,250,000.00" not in body
        assert "a@b.com" not in body
        assert response.json()["error"]["code"] == ErrorCode.INTERNAL_ERROR


class TestOpenApi:
    def test_the_spec_is_published(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        assert spec["info"]["title"] == "Customer 360 Intelligence Platform"
        assert "/health" in spec["paths"]
        assert "/ready" in spec["paths"]


class TestTraceExport:
    """The gate's telemetry assertion, made against what the exporter actually receives."""

    @pytest.fixture
    def exported_spans(self, client: TestClient) -> InMemorySpanExporter:
        telemetry = get_telemetry()
        assert telemetry is not None
        assert telemetry.tracer_provider is not None

        exporter = InMemorySpanExporter()
        telemetry.tracer_provider.add_span_processor(
            AllowlistSpanProcessor(SimpleSpanProcessor(exporter), telemetry.allowlist)
        )
        client.get("/health")
        telemetry.tracer_provider.force_flush()
        return exporter

    def test_a_request_produces_a_server_span(self, exported_spans: InMemorySpanExporter) -> None:
        spans = exported_spans.get_finished_spans()
        assert spans, "an HTTP request must produce at least one span"
        assert any("/health" in span.name for span in spans)

    def test_the_span_carries_the_correlation_id(
        self, exported_spans: InMemorySpanExporter
    ) -> None:
        correlation_ids = {
            (span.attributes or {}).get(SpanAttr.CORRELATION_ID)
            for span in exported_spans.get_finished_spans()
        }
        assert any(isinstance(value, str) and len(value) == 32 for value in correlation_ids)

    def test_no_disallowed_attribute_is_exported(
        self, exported_spans: InMemorySpanExporter
    ) -> None:
        telemetry = get_telemetry()
        assert telemetry is not None
        for span in exported_spans.get_finished_spans():
            rejected = telemetry.allowlist.rejected_keys(span.attributes)
            assert rejected == (), f"{span.name} exported disallowed attributes: {rejected}"

    def test_no_url_or_query_attribute_survives(self, exported_spans: InMemorySpanExporter) -> None:
        """A path carries the customer ID and a query string carries search terms."""
        for span in exported_spans.get_finished_spans():
            keys = set(span.attributes or {})
            assert not {"url.full", "url.path", "url.query", "http.target"} & keys
