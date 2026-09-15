"""Report-generation spans and metrics (task 18.7).

Mirrors the signals telemetry (:mod:`c360.signals.detect`): a lazily-created counter labelled by
``report_type`` and ``outcome`` — both bounded closed sets on the metric-label allowlist — records
how many report runs of each kind succeeded or failed. No customer id, book, title or monetary value
is ever a label or a span attribute (design §18.8); the whole point of routing labels through
``scrub_labels`` is that a stray key is dropped rather than exported.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from c360.core.telemetry import SpanAttr, get_meter, get_tracer, scrub_labels

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.trace import Span

_TRACER_NAME = "c360.reports"


class _ReportMetrics:
    """Lazily-created report instruments, so a disabled telemetry pipeline costs nothing."""

    __slots__ = ("_runs",)

    def __init__(self) -> None:
        meter = get_meter(_TRACER_NAME)
        self._runs = meter.create_counter(
            "c360.reports.runs",
            description="Report runs, labelled by report type and outcome.",
        )

    def record(self, *, report_type: str, outcome: str) -> None:
        self._runs.add(1, scrub_labels({"report_type": report_type, "outcome": outcome}))


_metrics: _ReportMetrics | None = None


def _report_metrics() -> _ReportMetrics:
    global _metrics  # noqa: PLW0603 - process-wide lazy singleton, mirrors signals
    if _metrics is None:
        _metrics = _ReportMetrics()
    return _metrics


def record_run(*, report_type: str, outcome: str) -> None:
    """Count one report run by type and outcome (task 18.7)."""
    _report_metrics().record(report_type=report_type, outcome=outcome)


@contextmanager
def report_span(report_type: str) -> Iterator[Span]:
    """A span around a report generation, carrying only the (bounded) report type."""
    tracer = get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span("generate_report") as span:
        span.set_attribute(SpanAttr.REPORT_TYPE, report_type)
        yield span


__all__ = ["record_run", "report_span"]
