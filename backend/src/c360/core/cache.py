"""A small, Redis-ready cache interface shared by every caching tier (task 16.2).

Design §13.1 lists four caches with different lifetimes and payloads:

* HTTP reference-data caching via ``ETag`` / ``Cache-Control`` (handled at the response layer, see
  :mod:`c360.api.caching`),
* a 60-second read-model TTL cache for deterministic reads,
* the 15-minute agent-output cache (:class:`c360.agents.cache.AgentCache`),
* the content-hashed embedding cache (:class:`c360.knowledge.embeddings.EmbeddingCache`).

The three in-process caches share one thing that must be swappable for Redis without touching a
call site: a get / put / invalidate contract keyed by an opaque string. This module defines that
contract as a :class:`~typing.Protocol` and ships the in-process TTL implementation. A Redis-backed
cache satisfies the same protocol — ``GET`` / ``SETEX`` / ``DEL`` / ``FLUSHDB`` map onto the four
methods — so wiring it in is a construction-time choice, not a code change at the point of use.

The value type is generic so each tier keeps its own payload (an ``AgentResult``, a serialized read
model, a vector) without this module knowing about any of them. The in-process implementation is
thread-safe because the API serves requests concurrently over one process-wide cache.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Cache[V](Protocol):
    """The get / put / invalidate contract every cache tier implements (task 16.2).

    A Redis-backed cache implements the same four methods, so a call site depending on this
    protocol is agnostic to whether the store is an in-process dict or a shared Redis instance.
    """

    def get(self, key: str) -> V | None:
        """Return the cached value for ``key``, or ``None`` on a miss or expiry."""
        ...

    def put(self, key: str, value: V) -> None:
        """Store ``value`` under ``key`` with the cache's configured TTL."""
        ...

    def invalidate(self, key: str) -> None:
        """Drop a single ``key`` if present."""
        ...

    def invalidate_all(self) -> int:
        """Drop every entry and return how many were dropped."""
        ...


class TtlCache[V]:
    """An in-process, thread-safe TTL cache implementing :class:`Cache`.

    The store is a plain dict of ``key -> (expires_at, value)`` keyed on a monotonic clock, so a
    wall-clock change cannot resurrect an expired entry. An expired entry is treated as a miss and
    dropped on access rather than swept on a timer — the working set is small and bounded by the
    number of distinct keys a short TTL admits, so lazy eviction is enough and needs no background
    thread. A zero (or negative) TTL disables the cache: every ``get`` misses and every ``put`` is a
    no-op, which is how a test or a deployment turns a tier off without a separate flag.
    """

    __slots__ = ("_lock", "_store", "_ttl_s")

    def __init__(self, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._store: dict[str, tuple[float, V]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> V | None:
        if self._ttl_s <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._store[key]
                return None
            return value

    def put(self, key: str, value: V) -> None:
        if self._ttl_s <= 0:
            return
        expires_at = time.monotonic() + self._ttl_s
        with self._lock:
            self._store[key] = (expires_at, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def invalidate_all(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
        return count


__all__ = ["Cache", "TtlCache"]
