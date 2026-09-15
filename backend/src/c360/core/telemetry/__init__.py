"""OpenTelemetry bootstrap, span attribute allowlist and telemetry helpers."""

from c360.core.telemetry.allowlist import (
    ALLOWED_SPAN_ATTRIBUTE_PREFIXES,
    ALLOWED_SPAN_ATTRIBUTES,
    PROMPT_CONTENT_ATTRIBUTES,
    SpanAttributeAllowlist,
)
from c360.core.telemetry.attributes import CustomerHasher, SpanAttr
from c360.core.telemetry.bootstrap import (
    Telemetry,
    current_trace_id,
    get_meter,
    get_telemetry,
    get_tracer,
    instrument_app,
    setup_telemetry,
    shutdown_telemetry,
)
from c360.core.telemetry.cost import CostEstimator, ModelPrice
from c360.core.telemetry.labels import (
    ALLOWED_METRIC_LABELS,
    label_permitted,
    rejected_labels,
    scrub_labels,
)
from c360.core.telemetry.metrics import (
    PlatformMetrics,
    breaker_transition_hook,
    get_metrics,
    reset_metrics,
    setup_metrics,
)
from c360.core.telemetry.processor import AllowlistSpanProcessor

__all__ = [
    "ALLOWED_METRIC_LABELS",
    "ALLOWED_SPAN_ATTRIBUTES",
    "ALLOWED_SPAN_ATTRIBUTE_PREFIXES",
    "PROMPT_CONTENT_ATTRIBUTES",
    "AllowlistSpanProcessor",
    "CostEstimator",
    "CustomerHasher",
    "ModelPrice",
    "PlatformMetrics",
    "SpanAttr",
    "SpanAttributeAllowlist",
    "Telemetry",
    "breaker_transition_hook",
    "current_trace_id",
    "get_meter",
    "get_metrics",
    "get_telemetry",
    "get_tracer",
    "instrument_app",
    "label_permitted",
    "rejected_labels",
    "reset_metrics",
    "scrub_labels",
    "setup_metrics",
    "setup_telemetry",
    "shutdown_telemetry",
]
