"""Task 0.5: OpenTelemetry bootstrap and the span attribute allowlist.

The allowlist assertions are the Phase 0 instalment of the telemetry-leakage test that design §17
names as one of the four tests carrying disproportionate weight. It is written now, against the
processor, so that every later phase inherits it.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from c360.core.telemetry import (
    ALLOWED_SPAN_ATTRIBUTES,
    PROMPT_CONTENT_ATTRIBUTES,
    AllowlistSpanProcessor,
    CustomerHasher,
    SpanAttr,
    SpanAttributeAllowlist,
    current_trace_id,
    setup_telemetry,
    shutdown_telemetry,
)
from tests.conftest import make_settings


class TestAllowlist:
    def test_operational_attributes_are_permitted(self) -> None:
        allowlist = SpanAttributeAllowlist()
        for key in (
            "http.request.method",
            "http.response.status_code",
            "http.route",
            "gen_ai.request.model",
            "gen_ai.usage.input_tokens",
            "db.system",
            "exception.type",
            SpanAttr.CORRELATION_ID,
            SpanAttr.CUSTOMER_HASH,
            SpanAttr.AGENT,
            SpanAttr.TOOL,
        ):
            assert allowlist.permits(key), key

    def test_identifying_and_monetary_attributes_are_refused(self) -> None:
        allowlist = SpanAttributeAllowlist()
        for key in (
            "customer_id",
            "c360.customer_id",
            "customer.email",
            "account.number",
            "balance_cents",
            "net_worth_cents",
            "url.full",
            "url.path",
            "url.query",
            "http.target",
            "db.statement",
            "db.query.text",
            "exception.message",
            "exception.stacktrace",
        ):
            assert not allowlist.permits(key), key

    def test_prompt_content_is_refused_by_default(self) -> None:
        allowlist = SpanAttributeAllowlist()
        for key in PROMPT_CONTENT_ATTRIBUTES:
            assert not allowlist.permits(key), key

    def test_prompt_content_is_permitted_only_when_explicitly_enabled(self) -> None:
        allowlist = SpanAttributeAllowlist(allow_prompt_content=True)
        assert allowlist.permits("gen_ai.prompt")

    def test_no_prefix_rule_widens_the_allowlist(self) -> None:
        """A prefix rule is how an allowlist quietly becomes a denylist."""
        allowlist = SpanAttributeAllowlist()
        assert not allowlist.permits("c360.anything_new")
        assert not allowlist.permits("gen_ai.something_unreviewed")

    def test_apply_and_rejected_keys_agree(self) -> None:
        allowlist = SpanAttributeAllowlist()
        attributes = {"http.route": "/health", "customer_id": "CUST-1", "balance_cents": 1}
        assert allowlist.apply(attributes) == {"http.route": "/health"}
        assert set(allowlist.rejected_keys(attributes)) == {"customer_id", "balance_cents"}

    def test_empty_attributes_are_handled(self) -> None:
        allowlist = SpanAttributeAllowlist()
        assert allowlist.apply(None) == {}
        assert allowlist.rejected_keys(None) == ()

    def test_the_allowlist_contains_no_content_keys(self) -> None:
        assert not (ALLOWED_SPAN_ATTRIBUTES & PROMPT_CONTENT_ATTRIBUTES)


class TestAllowlistProcessor:
    def _provider(self) -> tuple[TracerProvider, InMemorySpanExporter, AllowlistSpanProcessor]:
        exporter = InMemorySpanExporter()
        processor = AllowlistSpanProcessor(SimpleSpanProcessor(exporter))
        provider = TracerProvider()
        provider.add_span_processor(processor)
        return provider, exporter, processor

    def test_disallowed_attributes_never_reach_the_exporter(self) -> None:
        provider, exporter, processor = self._provider()
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("db.query") as span:
            span.set_attribute("db.system", "sqlite")
            span.set_attribute(SpanAttr.DB_STATEMENT_ID, "txn_category_rollup")
            span.set_attribute("db.statement", "SELECT * FROM customer WHERE email = 'a@b.com'")
            span.set_attribute("customer_id", "CUST-0001")
            span.set_attribute("balance_cents", 4_500_00)

        provider.force_flush()
        exported = exporter.get_finished_spans()[0]
        attributes = dict(exported.attributes or {})

        assert attributes == {
            "db.system": "sqlite",
            SpanAttr.DB_STATEMENT_ID: "txn_category_rollup",
        }
        assert processor.dropped_keys == frozenset({"db.statement", "customer_id", "balance_cents"})

    def test_span_identity_and_status_survive_filtering(self) -> None:
        provider, exporter, _ = self._provider()
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("gen_ai.invoke_agent") as span:
            expected_trace_id = span.get_span_context().trace_id
            span.set_attribute("gen_ai.request.model", "a.model-id:0")
            span.set_attribute("prompt", "leak me")

        provider.force_flush()
        exported = exporter.get_finished_spans()[0]
        exported_context = exported.get_span_context()

        assert exported.name == "gen_ai.invoke_agent"
        assert exported_context is not None
        assert exported_context.trace_id == expected_trace_id
        assert exported.start_time is not None
        assert exported.end_time is not None
        assert exported.instrumentation_scope is not None
        assert dict(exported.attributes or {}) == {"gen_ai.request.model": "a.model-id:0"}

    def test_event_attributes_are_filtered_too(self) -> None:
        provider, exporter, _ = self._provider()
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("agent.claim_validate") as span:
            span.add_event(
                "claim_rejected",
                attributes={"c360.claim_validation": "numeric_mismatch", "balance": 100},
            )

        provider.force_flush()
        event = exporter.get_finished_spans()[0].events[0]
        assert dict(event.attributes or {}) == {"c360.claim_validation": "numeric_mismatch"}

    def test_clean_spans_pass_through_untouched(self) -> None:
        provider, exporter, processor = self._provider()
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("http.request") as span:
            span.set_attribute("http.route", "/health")

        provider.force_flush()
        assert dict(exporter.get_finished_spans()[0].attributes or {}) == {"http.route": "/health"}
        assert processor.dropped_keys == frozenset()

    def test_lifecycle_is_delegated(self) -> None:
        provider, _, processor = self._provider()
        assert processor.force_flush() is True
        provider.shutdown()


class TestBootstrap:
    def test_disabled_telemetry_installs_no_provider(self) -> None:
        telemetry = setup_telemetry(make_settings(otel_enabled="false"))
        assert telemetry.enabled is False
        assert telemetry.tracer_provider is None
        assert telemetry.meter_provider is None
        shutdown_telemetry()

    def test_enabled_telemetry_installs_providers_and_resource_attributes(self) -> None:
        telemetry = setup_telemetry(make_settings(otel_service_name="c360-api-under-test"))
        assert telemetry.enabled is True
        assert telemetry.tracer_provider is not None
        assert telemetry.meter_provider is not None

        resource = telemetry.tracer_provider.resource.attributes
        assert resource["service.name"] == "c360-api-under-test"
        assert resource["service.namespace"] == "c360"
        assert resource["deployment.environment.name"] == "local"
        assert resource["service.version"]
        shutdown_telemetry()

    def test_setup_is_idempotent(self) -> None:
        settings = make_settings()
        first = setup_telemetry(settings)
        assert setup_telemetry(settings) is first
        shutdown_telemetry()

    def test_sampler_argument_is_honoured(self) -> None:
        telemetry = setup_telemetry(make_settings(otel_traces_sampler_arg="0.25"))
        assert telemetry.tracer_provider is not None
        assert "0.25" in str(telemetry.tracer_provider.sampler.get_description())
        shutdown_telemetry()

    def test_console_exporter_can_be_selected_without_a_collector(self) -> None:
        telemetry = setup_telemetry(
            make_settings(otel_traces_exporter="console", otel_metrics_exporter="console")
        )
        assert telemetry.enabled is True
        shutdown_telemetry()

    def test_prompt_capture_flag_propagates_to_the_allowlist(self) -> None:
        telemetry = setup_telemetry(make_settings(otel_capture_prompt_content="true"))
        assert telemetry.allowlist.permits("gen_ai.prompt")
        shutdown_telemetry()

    def test_shutdown_is_safe_when_nothing_was_configured(self) -> None:
        shutdown_telemetry()

    def test_current_trace_id_outside_a_span(self) -> None:
        assert current_trace_id() is None

    def test_current_trace_id_inside_a_span(self, span_exporter: InMemorySpanExporter) -> None:
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("unit"):
            trace_id = current_trace_id()
        assert trace_id is not None
        assert len(trace_id) == 32
        assert int(trace_id, 16) > 0


class TestCustomerHasher:
    def test_hashing_is_disabled_without_a_salt(self) -> None:
        hasher = CustomerHasher("")
        assert hasher.enabled is False
        assert hasher.hash("CUST-0001") is None

    def test_hash_is_stable_and_does_not_contain_the_input(self) -> None:
        hasher = CustomerHasher("a-deployment-scoped-salt")
        digest = hasher.hash("CUST-0001")
        assert digest is not None
        assert len(digest) == 16
        assert "CUST" not in digest
        assert hasher.hash("CUST-0001") == digest

    def test_different_salts_produce_different_pseudonyms(self) -> None:
        assert CustomerHasher("salt-one").hash("CUST-0001") != CustomerHasher("salt-two").hash(
            "CUST-0001"
        )
