"""Idle-session tracking (task 4.2, requirement 12.10).

An access token already dies 15 minutes after issue, so a *lost* token is bounded without any
server state. What a TTL cannot express is requirement 12.10's rule: a session that has been
**idle** past the configured window is terminated even if a still-valid token is presented. That
needs one fact a stateless token cannot carry — the time of the last request on the session — so
this module keeps it.

Per-instance, on purpose
------------------------

The store is an in-process dictionary, not a shared cache. Design §12.3 already accepts per-instance
state for the audit database and §13.5 requires the API tier to scale with **no sticky-session
dependency**, so a session pinned to one instance would break the load-balancer contract. The
reconciliation is deliberate: idle-timeout is enforced per instance as a *tightening* — an instance
that has not seen a session treats the token's own age as the activity floor, so the worst case is
that a session stays alive up to the token TTL after a load-balancer reshuffle, never that a
genuinely idle session is kept alive. The security property (an idle session cannot outlive the
window on the instance serving it) holds without shared state; a production deployment that wants
global idle-tracking swaps this for a shared store behind the same interface.

The store is bounded and self-pruning so it cannot grow without limit under a stream of distinct
subjects, and every method is safe to call from the worker threads the ASGI server dispatches on.
"""

from __future__ import annotations

import threading
import time
from typing import Final

#: Hard cap on tracked sessions. Past this, the oldest are pruned. Sized well above any realistic
#: concurrent-user count for a single instance (requirement 13.4: 100 concurrent users) so pruning
#: is a safety valve, not a normal path.
_MAX_TRACKED_SESSIONS: Final = 10_000


class IdleSessionTracker:
    """Tracks last-activity per session subject and decides whether a session has gone idle."""

    __slots__ = ("_idle_timeout_s", "_last_seen", "_lock")

    def __init__(self, *, idle_timeout_s: int) -> None:
        self._idle_timeout_s = idle_timeout_s
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_expired(self, subject: str, *, token_issued_at: int, now: float | None = None) -> bool:
        """Whether ``subject``'s session has been idle beyond the window.

        ``token_issued_at`` is the activity floor for a session this instance has not seen before —
        a token freshly issued 10 seconds ago is not idle even if this instance has no record of
        it, which is what keeps a load-balancer reshuffle from spuriously logging a user out.
        """
        current = time.time() if now is None else now
        with self._lock:
            last = self._last_seen.get(subject)
        floor = max(float(token_issued_at), last if last is not None else 0.0)
        return (current - floor) > self._idle_timeout_s

    def touch(self, subject: str, *, now: float | None = None) -> None:
        """Record activity for ``subject`` at the current time."""
        current = time.time() if now is None else now
        with self._lock:
            self._last_seen[subject] = current
            if len(self._last_seen) > _MAX_TRACKED_SESSIONS:
                self._prune_locked()

    def forget(self, subject: str) -> None:
        """Drop a session's activity record, e.g. on explicit logout."""
        with self._lock:
            self._last_seen.pop(subject, None)

    def _prune_locked(self) -> None:
        """Drop the least-recently-seen half of the table. Caller holds the lock."""
        ordered = sorted(self._last_seen.items(), key=lambda item: item[1])
        keep = ordered[len(ordered) // 2 :]
        self._last_seen = dict(keep)
