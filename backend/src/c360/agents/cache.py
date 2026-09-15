"""Agent output caching (task 8.8, design §8.7).

A dashboard narrative is expensive to generate and stable between recomputes, so an agent's output
is cached and re-served for a short window. The key is what makes the cache *correct* rather than
merely fast — it folds in everything that could legitimately change the narrative, so a stale entry
is never served after any input that matters has changed:

    sha256(agent, customer_id, role, as_of_date, fact_fingerprint, knowledge_fingerprint,
           formula_version, prompt_version, model_id)

Why each component is in the key (design §8.7):

* ``role`` — two roles see different masked inputs and must not share an output.
* ``fact_fingerprint`` / ``knowledge_fingerprint`` — the actual facts and passages the agent saw;
  a recompute that changes a figure changes the fingerprint and so misses the cache.
* ``formula_version`` — a scoring-formula change (``fhs-v2``) must invalidate a narrative built on
  the old score even if the raw facts are unchanged.
* ``prompt_version`` / ``model_id`` — a prompt edit or a model switch must invalidate stale
  narratives, or an evaluation improvement would be invisible in a running system.

Entries carry a TTL (default 15 minutes, ``AGENT_CACHE_TTL_S``); :meth:`invalidate_all` clears the
whole cache and is what the admin recompute path calls, since a recompute can change any customer's
facts. ``cache_hit`` is set to ``True`` on the copy returned from the cache, so the client can label
the card as cached beside the generation timestamp.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.agents.provider import PassageRef
    from c360.agents.result import AgentResult
    from c360.tools.facts import FactTable


def fact_fingerprint(facts: FactTable) -> str:
    """A stable hash over the facts an agent saw: field, value and provenance, in table order.

    Order is table order (the ``F``-ids), which ``load_context`` assigns deterministically, so the
    fingerprint is reproducible for the same underlying data and changes the moment any figure does.
    """
    hasher = hashlib.sha256()
    for fact in facts.facts:
        hasher.update(f"{fact.entity_type}|{fact.field}|{fact.value}|{fact.as_of}\n".encode())
    return hasher.hexdigest()


def knowledge_fingerprint(passages: Sequence[PassageRef]) -> str:
    """A stable hash over the retrieved passages: doc, section and text, in retrieval order.

    Empty passages hash to a fixed digest, so "retrieved nothing" is a distinct, cacheable state
    from "retrieved these three chunks".
    """
    hasher = hashlib.sha256()
    for passage in passages:
        hasher.update(f"{passage.doc_id}|{passage.section_path}|{passage.text}\n".encode())
    return hasher.hexdigest()


def agent_cache_key(
    *,
    agent: str,
    customer_id: str,
    role: str,
    as_of: str,
    fact_fp: str,
    knowledge_fp: str,
    formula_version: str,
    prompt_version: str,
    model_id: str,
) -> str:
    """The composite cache key (§8.7). Every component that could change the output is in it."""
    payload = "\u241f".join(
        (
            agent,
            customer_id,
            role,
            as_of,
            fact_fp,
            knowledge_fp,
            formula_version,
            prompt_version,
            model_id,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AgentCache:
    """A TTL cache of :class:`AgentResult`, keyed by :func:`agent_cache_key` (task 8.8).

    Thread-safe: the dashboard graph runs agents concurrently, and the process-wide cache is shared
    across requests, so reads and writes take a lock. The store is a plain dict of
    ``key -> (expires_at, result)``; an expired entry is treated as a miss and dropped on access.

    This is one of the tiers behind the shared :class:`c360.core.cache.Cache` protocol (task 16.2):
    ``get`` / ``put`` / ``invalidate_all`` map directly onto it, so a Redis-backed store swaps in
    without touching the dashboard graph. It keeps its own class rather than reusing
    :class:`c360.core.cache.TtlCache` only because of the ``cache_hit`` flag — the copy returned by
    ``get`` is marked served-from-cache so the UI can label the card, which the generic value cache
    has no concept of.
    """

    __slots__ = ("_lock", "_store", "_ttl_s")

    def __init__(self, ttl_s: int) -> None:
        self._ttl_s = ttl_s
        self._store: dict[str, tuple[float, AgentResult]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> AgentResult | None:
        """Return the cached result for ``key`` with ``cache_hit=True``, or ``None`` on miss/expiry.

        A zero TTL disables the cache entirely (every get is a miss), which is how a test or a
        deployment turns caching off without a separate flag.
        """
        if self._ttl_s <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, result = entry
            if expires_at <= now:
                del self._store[key]
                return None
        return result.model_copy(update={"cache_hit": True})

    def put(self, key: str, result: AgentResult) -> None:
        """Store ``result`` under ``key`` with the configured TTL. A zero TTL stores nothing."""
        if self._ttl_s <= 0:
            return
        expires_at = time.monotonic() + self._ttl_s
        # Always store with cache_hit=False; the flag is flipped on the copy returned by `get`, so a
        # served-from-cache result is distinguishable from the first computed one.
        stored = result.model_copy(update={"cache_hit": False})
        with self._lock:
            self._store[key] = (expires_at, stored)

    def invalidate_all(self) -> int:
        """Drop every entry and return how many were dropped. Called by the recompute path."""
        with self._lock:
            count = len(self._store)
            self._store.clear()
        return count


__all__ = [
    "AgentCache",
    "agent_cache_key",
    "fact_fingerprint",
    "knowledge_fingerprint",
]
