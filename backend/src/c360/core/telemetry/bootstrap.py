"""OpenTelemetry SDK bootstrap.

Traces and metrics over OTLP to a collector, exactly as design §13.1 requires: the application is
backend-agnostic and swapping Jaeger/Prometheus for X-Ray/CloudWatch is a collector change, never
an application change (requirement 18.13).

The allowlist processor is installed here, on the first line of the pipeline, so it is impossible
to add instrumentation later that bypasses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased

from c360 import __version__
from c360.core.config import Settings
from c360.core.telemetry.allowlist import SpanAttributeAllowlist
from c360.core.telemetry.processor import AllowlistSpanProcessor

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Routes that must not create spans. `/metrics` is scraped continuously and would otherwise
#: dominate the trace volume with no diagnostic value. `/health` stays instrumented on purpose:
#: it is the Phase 0 gate's proof that the pipeline works.
EXCLUDED_URLS: Final = "metrics"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """Handles for the configured providers, so shutdown can flush them deterministically."""

    tracer_provider: TracerProvider | None
    meter_provider: MeterProvider | None
    allowlist: SpanAttributeAllowlist
    enabled: bool


class _TelemetryState:
    """Holder for the configured providers, so no function needs a ``global`` statement."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: Telemetry | None = None


_state = _TelemetryState()


def _build_resource(settings: Settings) -> Resource:
    """Resource attributes identifying this service in every exported signal.

    Written as literal strings rather than pulled from ``semconv``: the resource attribute
    constants have churned across releases (``deployment.environment`` to
    ``deployment.environment.name``), and the wire format is what a backend actually keys on.
    """
    return Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": __version__,
            "service.namespace": "c360",
            "deployment.environment.name": str(settings.environment),
        }
    )


def _grpc_endpoint(raw: str) -> tuple[str, bool]:
    """Split an OTLP endpoint into (target, insecure).

    The gRPC exporter wants a host:port target; the scheme decides TLS. Passing ``http://`` through
    unchanged works by accident on some versions and not others, so it is normalised here.
    """
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    target = parsed.netloc or parsed.path
    insecure = parsed.scheme != "https"
    return target, insecure


def _span_exporter(settings: Settings) -> SpanExporter | None:
    match settings.otel_traces_exporter:
        case "otlp":
            endpoint, insecure = _grpc_endpoint(settings.otel_exporter_otlp_endpoint)
            return OTLPSpanExporter(endpoint=endpoint, insecure=insecure)
        case "console":
            return ConsoleSpanExporter()
        case _:
            return None


def _metric_exporter(settings: Settings) -> MetricExporter | None:
    match settings.otel_metrics_exporter:
        case "otlp":
            endpoint, insecure = _grpc_endpoint(settings.otel_exporter_otlp_endpoint)
            return OTLPMetricExporter(endpoint=endpoint, insecure=insecure)
        case "console":
            return ConsoleMetricExporter()
        case _:
            return None


def setup_telemetry(settings: Settings) -> Telemetry:
    """Configure tracing and metrics. Idempotent for the lifetime of the process."""
    if _state.value is not None:
        return _state.value

    allowlist = SpanAttributeAllowlist(
        allow_prompt_content=settings.otel_capture_prompt_content,
    )

    if not settings.otel_enabled:
        _state.value = Telemetry(
            tracer_provider=None,
            meter_provider=None,
            allowlist=allowlist,
            enabled=False,
        )
        return _state.value

    resource = _build_resource(settings)

    sampler = (
        ALWAYS_ON
        if settings.otel_traces_sampler_arg >= 1.0
        else ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_arg))
    )
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)

    span_exporter = _span_exporter(settings)
    if span_exporter is not None:
        tracer_provider.add_span_processor(
            AllowlistSpanProcessor(BatchSpanProcessor(span_exporter), allowlist)
        )
    trace.set_tracer_provider(tracer_provider)

    readers: list[MetricReader] = []
    metric_exporter = _metric_exporter(settings)
    if metric_exporter is not None:
        readers.append(
            PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=settings.otel_metric_export_interval_ms,
            )
        )
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(meter_provider)

    _state.value = Telemetry(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        allowlist=allowlist,
        enabled=True,
    )
    return _state.value


def get_telemetry() -> Telemetry | None:
    """Return the configured providers, or ``None`` before :func:`setup_telemetry` has run."""
    return _state.value


def shutdown_telemetry() -> None:
    """Flush and shut down the providers."""
    telemetry = _state.value
    if telemetry is None:
        return
    if telemetry.tracer_provider is not None:
        telemetry.tracer_provider.shutdown()
    if telemetry.meter_provider is not None:
        telemetry.meter_provider.shutdown()
    _state.value = None


def instrument_app(app: FastAPI, settings: Settings) -> None:
    """Attach ASGI instrumentation to the application.

    Imported lazily: the instrumentation package pulls in a sizeable dependency graph that CLI
    entry points (seed, recompute, eval) have no use for.
    """
    if not settings.otel_enabled:
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

    telemetry = setup_telemetry(settings)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=telemetry.tracer_provider,
        meter_provider=telemetry.meter_provider,
        excluded_urls=EXCLUDED_URLS,
    )


def current_trace_id() -> str | None:
    """Return the active trace ID as a 32-character hex string, or ``None`` outside a trace."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)


def get_meter(name: str) -> metrics.Meter:
    """Return a meter for a module.

    Resolved per call for the same reason :func:`get_tracer` is: a meter captured at import time
    binds to whatever provider existed then, and the CLI entry points import application modules
    before telemetry is configured. A provider lookup is cheap; a stale no-op meter is a silently
    missing metric.
    """
    return metrics.get_meter(name)


def get_tracer(name: str) -> trace.Tracer:
    """Return a tracer for a module."""
    return trace.get_tracer(name)
