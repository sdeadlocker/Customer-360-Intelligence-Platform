"""Structured JSON logging with correlation IDs and mandatory redaction.

Requirement 13.8 wants a correlation ID on every line; requirement 18.2 wants log lines, metrics
and traces to share an identifier; requirement 18.8 forbids PII, monetary values and prompt content
in any emitted signal.

The redaction filter is attached to the **handler**, not to individual loggers. A filter on a
logger is bypassed by any code that logs through a different logger; a filter on the only handler
is not bypassable from application code, which is the property this guarantee needs.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from opentelemetry import trace

from c360.core.config import Settings
from c360.core.context import get_correlation_id
from c360.core.redaction import redact_mapping, redact_text

#: Attributes present on every ``LogRecord``; anything else was passed as ``extra``.
_STANDARD_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class _LoggingState:
    """Holder for the one piece of module state, so no function needs a ``global`` statement."""

    __slots__ = ("configured",)

    def __init__(self) -> None:
        self.configured = False


_state = _LoggingState()


class ContextFilter(logging.Filter):
    """Attach correlation and trace identifiers to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = trace.format_trace_id(span_context.trace_id)
            record.span_id = trace.format_span_id(span_context.span_id)
        else:
            record.trace_id = "-"
            record.span_id = "-"
        return True


class RedactionFilter(logging.Filter):
    """Redact identifying and monetary content from messages and structured fields.

    Interpolation happens here rather than in the formatter: ``record.getMessage()`` is what would
    reach the output, so it is the string that must be scanned. The already-formatted result is
    written back and the arguments dropped, so no downstream formatter can re-expose the raw values.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # pragma: no cover - malformed log call
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = None

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
        }
        for key, redacted in redact_mapping(extras).items():
            record.__dict__[key] = redacted

        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    """Render records as single-line JSON."""

    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service_name,
            "environment": self._environment,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
            "source": f"{record.module}:{record.lineno}",
        }

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            if key in {"correlation_id", "trace_id", "span_id"}:
                continue
            payload[key] = value

        return json.dumps(payload, default=str, separators=(",", ":"))

    def formatException(self, ei: Any) -> str:  # noqa: N802 - stdlib signature
        return redact_text(super().formatException(ei))


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line output for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s [%(correlation_id)s] %(message)s",
            datefmt="%H:%M:%S",
        )

    def formatException(self, ei: Any) -> str:  # noqa: N802 - stdlib signature
        return redact_text(super().formatException(ei))


def configure_logging(settings: Settings, *, force: bool = False) -> None:
    """Install the root handler, filters and formatter. Idempotent.

    Uvicorn installs its own handlers on ``uvicorn.access`` and ``uvicorn.error``; those are
    redirected to propagate into the root handler so that every line in the process — application
    or server — is JSON, correlated and redacted.
    """
    if _state.configured and not force:
        return

    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JsonFormatter(
            service_name=settings.otel_service_name,
            environment=str(settings.environment),
        )
    else:
        formatter = ConsoleFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    # Order matters: context first so identifiers exist, redaction last so nothing added
    # afterwards escapes it.
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers.clear()
        logger.propagate = True

    _state.configured = True


def reset_logging() -> None:
    """Forget that logging was configured. Test-support only."""
    _state.configured = False


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""
    return logging.getLogger(name)
