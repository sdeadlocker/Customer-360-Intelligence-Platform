"""Span processor that enforces the attribute allowlist before export.

Placed at the processor level rather than inside individual instrumentation so that it applies to
spans this codebase never wrote — FastAPI, SQLAlchemy, botocore and LangChain instrumentation all
pass through it. It wraps an inner processor (normally a ``BatchSpanProcessor``) and hands it a
filtered copy of the span, because :class:`~opentelemetry.sdk.trace.ReadableSpan` attributes are
immutable once the span has ended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor

from c360.core.telemetry.allowlist import SpanAttributeAllowlist

if TYPE_CHECKING:
    from opentelemetry.util.types import AttributeValue


class AllowlistSpanProcessor(SpanProcessor):
    """Delegating processor that strips non-allowlisted attributes from every span."""

    __slots__ = ("_allowlist", "_dropped_keys", "_inner")

    def __init__(
        self,
        inner: SpanProcessor,
        allowlist: SpanAttributeAllowlist | None = None,
    ) -> None:
        self._inner = inner
        self._allowlist = allowlist or SpanAttributeAllowlist()
        self._dropped_keys: set[str] = set()

    @property
    def dropped_keys(self) -> frozenset[str]:
        """Attribute keys dropped so far. Diagnostic aid for new instrumentation."""
        return frozenset(self._dropped_keys)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        self._inner.on_end(self._filter(span))

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)

    # ------------------------------------------------------------------ internals
    def _filter(self, span: ReadableSpan) -> ReadableSpan:
        attributes = span.attributes or {}
        rejected = self._allowlist.rejected_keys(attributes)
        events = tuple(span.events or ())
        event_rejects = any(self._allowlist.rejected_keys(event.attributes) for event in events)

        if not rejected and not event_rejects:
            return span

        self._dropped_keys.update(rejected)

        filtered_attributes = cast("dict[str, AttributeValue]", self._allowlist.apply(attributes))
        filtered_events = tuple(
            Event(
                name=event.name,
                attributes=cast(
                    "dict[str, AttributeValue]", self._allowlist.apply(event.attributes)
                ),
                timestamp=event.timestamp,
            )
            for event in events
        )

        return ReadableSpan(
            name=span.name,
            context=span.get_span_context(),
            parent=span.parent,
            resource=span.resource,
            attributes=filtered_attributes,
            events=filtered_events,
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )
