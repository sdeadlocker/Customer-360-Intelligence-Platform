"""Request-scoped context propagated through the call stack.

The correlation ID is generated at the edge (design §16) and must reach services, graph queries,
tool calls, retrieval, agent runs, audit records and log lines without being threaded through every
signature. ``contextvars`` gives that for free on asyncio and — because each worker thread gets a
copy of the context at submission time — across the aggregator's thread-pool fan-out too.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from c360.security.model import Principal

CORRELATION_ID_HEADER: Final = "X-Correlation-ID"

# Accept only what we would generate ourselves, plus plain hex of the same length. An inbound
# header is untrusted input: it lands in logs, spans and audit records, so it is validated rather
# than echoed.
_CORRELATION_ID_PATTERN: Final = re.compile(r"\A[0-9a-fA-F]{8,64}\Z|\A[0-9a-fA-F-]{36}\Z")

_correlation_id: ContextVar[str | None] = ContextVar("c360_correlation_id", default=None)


def new_correlation_id() -> str:
    """Generate a fresh correlation ID."""
    return uuid.uuid4().hex


def is_valid_correlation_id(value: str) -> bool:
    """Return whether an inbound correlation ID is safe to adopt verbatim."""
    return bool(_CORRELATION_ID_PATTERN.match(value))


def normalise_correlation_id(value: str | None) -> str:
    """Adopt a caller-supplied correlation ID when it is well formed, otherwise mint one."""
    if value is not None:
        candidate = value.strip()
        if is_valid_correlation_id(candidate):
            return candidate.lower().replace("-", "")
    return new_correlation_id()


def set_correlation_id(value: str) -> Token[str | None]:
    """Bind a correlation ID to the current context."""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the previous correlation ID binding."""
    _correlation_id.reset(token)


def get_correlation_id() -> str | None:
    """Return the correlation ID bound to the current context, if any."""
    return _correlation_id.get()


def require_correlation_id() -> str:
    """Return the bound correlation ID, minting one if the context is unbound.

    Background jobs (recompute, ingestion, evaluation) have no HTTP edge to generate an ID, and a
    log line without one is far worse than a synthesised one.
    """
    current = _correlation_id.get()
    if current is None:
        current = new_correlation_id()
        _correlation_id.set(current)
    return current


@contextmanager
def correlation_scope(value: str | None = None) -> Iterator[str]:
    """Bind a correlation ID for the duration of a block, then restore the previous value."""
    correlation_id = normalise_correlation_id(value)
    token = set_correlation_id(correlation_id)
    try:
        yield correlation_id
    finally:
        reset_correlation_id(token)


# ---------------------------------------------------------------- authenticated principal
#
# The principal is bound here for the same reason the correlation ID is: services, tools, the
# entitlement gate and the audit writer all need "who is asking" without it being threaded through
# every signature. It is set by the authentication middleware (task 4.2) after a token is validated
# and cleared at the end of the request. Reading it where none is bound is a programming error — a
# handler that runs behind the auth middleware always has one — so the accessor raises rather than
# returning ``None`` and letting an unauthenticated code path proceed silently.

_principal: ContextVar[Principal | None] = ContextVar("c360_principal", default=None)


def set_principal(principal: Principal) -> Token[Principal | None]:
    """Bind the authenticated principal to the current context."""
    return _principal.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    """Restore the previous principal binding."""
    _principal.reset(token)


def get_principal() -> Principal | None:
    """Return the bound principal, or ``None`` when the context is unauthenticated."""
    return _principal.get()


def require_principal() -> Principal:
    """Return the bound principal, raising when none is bound.

    A handler behind the authentication middleware always has one; reaching this with no principal
    means an authorization check is running on a path the middleware did not cover, which must fail
    loudly rather than default to some identity.
    """
    current = _principal.get()
    if current is None:
        raise LookupError("no authenticated principal is bound to the current context")
    return current


@contextmanager
def principal_scope(principal: Principal) -> Iterator[Principal]:
    """Bind a principal for the duration of a block, then restore the previous value.

    Used by the middleware per request, and by background jobs and tests that need a principal in
    context without an HTTP edge.
    """
    token = set_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(token)
