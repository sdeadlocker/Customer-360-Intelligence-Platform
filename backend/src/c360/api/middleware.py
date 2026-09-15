"""Edge middleware: correlation ID generation, propagation and access logging.

Requirement 13.8: a correlation ID is generated at the edge and propagated across API, graph and
agent calls. Requirement 18.2: logs, metrics and traces share an identifier. Both are satisfied
here — the ID is bound to a ``contextvar`` for the duration of the request and attached to the
active server span, which links the log stream and the trace without a custom mapping.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from c360.core.context import (
    CORRELATION_ID_HEADER,
    normalise_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from c360.core.logging import get_logger
from c360.core.telemetry import SpanAttr, current_trace_id

TRACE_ID_HEADER: Final = "X-Trace-Id"

_logger = get_logger(__name__)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation ID to the request context and echo it on the response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        correlation_id = normalise_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
        token = set_correlation_id(correlation_id)

        # The ASGI instrumentation's server span is already active here, so the correlation ID
        # lands on the span that a backend will show as the root of the request.
        span = trace.get_current_span()
        if span.get_span_context().is_valid:
            span.set_attribute(SpanAttr.CORRELATION_ID, correlation_id)

        try:
            response = await call_next(request)
        finally:
            reset_correlation_id(token)

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        trace_id = current_trace_id()
        if trace_id is not None:
            response.headers[TRACE_ID_HEADER] = trace_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured line per request.

    Only the templated route is logged, never the raw path or query string: a path carries the
    customer ID and a query string carries search terms (requirement 18.8).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            _logger.exception(
                "request failed",
                extra={
                    "http_method": request.method,
                    "http_route": _route_of(request),
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        _logger.info(
            "request completed",
            extra={
                "http_method": request.method,
                "http_route": _route_of(request),
                "http_status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    """Record the HTTP RED metrics for every request (task 10.2, design §13.3).

    Duration, request and error counters, and a per-route budget-breach counter — all labelled by
    the templated route, method and status, never the raw path (which carries the customer id).
    The FastAPI instrumentation also emits ``http.server.request.duration``; this instrument is the
    explicit, allowlist-scrubbed one the design's inventory names and the dashboards query.
    """

    def __init__(self, app: ASGIApp, *, budget_ms: float) -> None:
        super().__init__(app)
        self._budget_ms = budget_ms

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        from c360.core.telemetry import get_metrics  # noqa: PLC0415 - avoid import cycle at edge

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            metrics = get_metrics()
            if metrics is not None:
                metrics.record_http(
                    method=request.method,
                    route=_route_of(request),
                    status_code=status_code,
                    duration_ms=duration_ms,
                    over_budget=duration_ms > self._budget_ms,
                )


def _route_of(request: Request) -> str:
    """Return the templated route for a request, falling back to ``unmatched``."""
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(path_format, str):
        return path_format
    return "unmatched"
