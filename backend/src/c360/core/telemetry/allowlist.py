"""Span attribute allowlist.

Design §13.4: *span attributes pass an allowlist filter before export*, so that instrumentation
added later cannot leak by accident. An allowlist rather than a denylist is the whole point — a
denylist protects against the leaks you thought of, an allowlist protects against the ones you
did not.

Two consequences worth stating plainly, because both are deliberate:

* ``url.full``, ``url.path`` and ``url.query`` are **not** allowed. A path carries the customer ID
  and a query string carries search terms, which are customer data. ``http.route`` is the
  templated route and is what dashboards group by anyway (design §13.4 allowed labels).
* ``exception.message`` and ``exception.stacktrace`` are **not** allowed. An exception message in
  this domain routinely interpolates a balance or an identifier. ``exception.type`` survives, and
  the redacted detail is available on the correlated log line.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from c360.core.telemetry.attributes import SpanAttr

#: Attributes carrying model prompt or completion content. Allowed only when
#: ``OTEL_CAPTURE_PROMPT_CONTENT`` is explicitly enabled for local debugging.
PROMPT_CONTENT_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
    }
)

_HTTP_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "http.request.method",
        "http.response.status_code",
        "http.route",
        "network.protocol.name",
        "network.protocol.version",
        "server.address",
        "server.port",
        "http.method",  # legacy names still emitted by some instrumentations
        "http.status_code",
        "http.scheme",
    }
)

_GENAI_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.operation.name",
        "gen_ai.agent.name",
        "gen_ai.tool.name",
        "gen_ai.tool.type",
        "gen_ai.request.model",
        "gen_ai.request.max_tokens",
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
        "gen_ai.response.model",
        "gen_ai.response.id",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    }
)

_DB_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "db.system",
        "db.system.name",
        "db.operation.name",
        "db.collection.name",
        # db.statement / db.query.text are excluded: bound values would ride along.
    }
)

_PLATFORM_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "otel.status_code",
        "otel.library.name",
        "otel.library.version",
        "otel.scope.name",
        "otel.scope.version",
        "code.function",
        "code.namespace",
        "code.filepath",
        "code.lineno",
        "exception.type",
        "exception.escaped",
        "asgi.event.type",
        "thread.id",
        "thread.name",
    }
)

_C360_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        value
        for name, value in vars(SpanAttr).items()
        if not name.startswith("_") and isinstance(value, str)
    }
)

#: Every attribute key permitted on an exported span.
ALLOWED_SPAN_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    _HTTP_ATTRIBUTES | _GENAI_ATTRIBUTES | _DB_ATTRIBUTES | _PLATFORM_ATTRIBUTES | _C360_ATTRIBUTES
)

#: Prefixes permitted wholesale. Kept intentionally empty: a prefix rule is how an allowlist
#: quietly becomes a denylist. Add a key to :data:`ALLOWED_SPAN_ATTRIBUTES` instead.
ALLOWED_SPAN_ATTRIBUTE_PREFIXES: Final[tuple[str, ...]] = ()


class SpanAttributeAllowlist:
    """Decides which span attributes may be exported."""

    __slots__ = ("_allowed", "_prefixes")

    def __init__(
        self,
        *,
        allowed: Iterable[str] = ALLOWED_SPAN_ATTRIBUTES,
        prefixes: Iterable[str] = ALLOWED_SPAN_ATTRIBUTE_PREFIXES,
        allow_prompt_content: bool = False,
    ) -> None:
        keys = set(allowed)
        if allow_prompt_content:
            keys |= PROMPT_CONTENT_ATTRIBUTES
        else:
            keys -= PROMPT_CONTENT_ATTRIBUTES
        self._allowed = frozenset(keys)
        self._prefixes = tuple(prefixes)

    def permits(self, key: str) -> bool:
        """Return whether an attribute key may be exported."""
        if key in self._allowed:
            return True
        return any(key.startswith(prefix) for prefix in self._prefixes)

    def apply(self, attributes: Mapping[str, object] | None) -> dict[str, object]:
        """Return only the permitted entries of an attribute mapping."""
        if not attributes:
            return {}
        return {key: value for key, value in attributes.items() if self.permits(key)}

    def rejected_keys(self, attributes: Mapping[str, object] | None) -> tuple[str, ...]:
        """Return the keys that would be dropped. Used by the leakage test and diagnostics."""
        if not attributes:
            return ()
        return tuple(key for key in attributes if not self.permits(key))
