"""A small circuit breaker for the Bedrock-dependent paths (task 8.11, design §13.7).

The breaker turns a *repeatedly* failing dependency into an immediate, cheap fallback rather than
paying the timeout on every call. It has the three standard states:

* **closed** — calls flow through; failures are counted, successes reset the count.
* **open** — the failure count crossed the threshold; calls are short-circuited (the caller uses its
  fallback) without touching the dependency, until a cool-off passes.
* **half-open** — after the cool-off, one trial call is allowed; success closes the breaker, a
  failure re-opens it. This is what lets the system recover on its own when Bedrock comes back.

Three breakers exist in the design (§13.7), each with its own fallback: the *generation* breaker
falls back to the deterministic template renderer, the *embedding* breaker to lexical-only search,
and the *rerank* breaker to fusion order. This module is the mechanism; each call site owns its
fallback. It is deliberately tiny and dependency-free, and thread-safe because the dashboard runs
agents concurrently over one process-wide breaker.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Final


class BreakerState(StrEnum):
    """The breaker's state. Exposed so a metric/health check can report it (task 10.4)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


#: Sensible defaults: trip after this many consecutive failures, cool off for this long.
_DEFAULT_THRESHOLD: Final = 5
_DEFAULT_COOLOFF_S: Final = 30.0


class CircuitBreaker:
    """A consecutive-failure circuit breaker (task 8.11)."""

    __slots__ = (
        "_cooloff_s",
        "_failures",
        "_lock",
        "_on_transition",
        "_opened_at",
        "_state",
        "_threshold",
        "name",
    )

    def __init__(
        self,
        name: str,
        *,
        threshold: int = _DEFAULT_THRESHOLD,
        cooloff_s: float = _DEFAULT_COOLOFF_S,
        on_transition: Callable[[str, BreakerState], None] | None = None,
    ) -> None:
        self.name = name
        self._threshold = threshold
        self._cooloff_s = cooloff_s
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED
        self._lock = threading.Lock()
        # A dependency-free transition hook (task 10.4): the breaker stays free of a telemetry
        # import, and the wiring layer supplies a callback that records the breaker metric and
        # feeds the breaker-open alert. Fired outside the lock so a slow callback never holds it.
        self._on_transition = on_transition

    @property
    def state(self) -> BreakerState:
        """The current state, resolving an elapsed cool-off to half-open on read."""
        with self._lock:
            return self._current_state()

    def allow(self) -> bool:
        """Whether a call may proceed now.

        ``True`` when closed or half-open (a trial), ``False`` when open and still cooling off. A
        caller that gets ``False`` uses its fallback and does not touch the dependency.
        """
        with self._lock:
            return self._current_state() is not BreakerState.OPEN

    def record_success(self) -> None:
        """A successful call: reset the failure count and close the breaker."""
        transitioned = False
        with self._lock:
            was_closed = self._state is BreakerState.CLOSED
            self._failures = 0
            self._state = BreakerState.CLOSED
            transitioned = not was_closed
        if transitioned:
            self._notify(BreakerState.CLOSED)

    def record_failure(self) -> None:
        """A failed call: count it, and open the breaker once the threshold is reached.

        In half-open, a single failure re-opens immediately — the trial call did not recover the
        dependency, so there is no point letting the next caller try again before the next cool-off.
        """
        tripped = False
        with self._lock:
            if self._current_state() is BreakerState.HALF_OPEN:
                tripped = self._trip()
            else:
                self._failures += 1
                if self._failures >= self._threshold:
                    tripped = self._trip()
        if tripped:
            self._notify(BreakerState.OPEN)

    def _trip(self) -> bool:
        """Open the breaker; return whether this caused the transition. Caller holds the lock."""
        already_open = self._state is BreakerState.OPEN
        self._state = BreakerState.OPEN
        self._opened_at = time.monotonic()
        return not already_open

    def _notify(self, state: BreakerState) -> None:
        """Fire the transition hook outside the lock, swallowing any callback error.

        A telemetry hook must never break the breaker, so any exception it raises is suppressed —
        the breaker's own state has already been updated by the time this runs.
        """
        if self._on_transition is None:
            return
        with contextlib.suppress(Exception):
            self._on_transition(self.name, state)

    def _current_state(self) -> BreakerState:
        """Resolve OPEN -> HALF_OPEN when the cool-off has elapsed. Caller holds the lock."""
        if self._state is BreakerState.OPEN and (
            time.monotonic() - self._opened_at >= self._cooloff_s
        ):
            self._state = BreakerState.HALF_OPEN
        return self._state


__all__ = ["BreakerState", "CircuitBreaker"]
