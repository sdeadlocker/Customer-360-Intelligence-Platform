"""Breaker-wrapped retrieval dependencies: embedding and rerank (task 8.11, design §13.7).

Two of the three circuit breakers guard the *retrieval* path rather than generation, and their
fallbacks are owned by the retriever, not by these wrappers:

* :class:`BreakerEmbeddingProvider` wraps the query embedder. When its breaker is open it raises
  immediately (no Bedrock call); the retriever catches that in ``_embed`` and skips the semantic
  stage, so retrieval degrades to **lexical-only** (requirement 13.7). A real embedding failure is
  recorded against the breaker and re-raised for the same lexical-only degradation.
* :class:`BreakerReranker` wraps the reranker. When its breaker is open it returns the passages in
  **fusion order** unchanged; a rerank failure is recorded and also falls back to fusion order — the
  reranker contract already forbids failing the request, so the breaker only saves the round trip
  once the model is reliably down.

These live in the agents package because :class:`~c360.agents.breaker.CircuitBreaker` does, and the
knowledge layer must not depend upward on agents. They are wired in at service construction
(``c360.api.services``), which already sits above both layers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.agents.breaker import CircuitBreaker
    from c360.knowledge.embeddings import EmbeddingProvider, Vector
    from c360.knowledge.models import Passage
    from c360.knowledge.rerank import Reranker


class BreakerEmbeddingProvider:
    """An :class:`~c360.knowledge.embeddings.EmbeddingProvider` guarded by a circuit breaker."""

    __slots__ = ("_breaker", "_inner", "model_id")

    def __init__(self, inner: EmbeddingProvider, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker
        self.model_id = inner.model_id

    def embed(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        """Embed, or raise when the breaker is open so the retriever degrades to lexical-only."""
        if not self._breaker.allow():
            raise EmbeddingUnavailableError("embedding circuit breaker is open")
        try:
            vectors: list[Vector] = self._inner.embed(texts, dimensions=dimensions)
        except Exception:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return vectors


class BreakerReranker:
    """A :class:`~c360.knowledge.rerank.Reranker` guarded by a circuit breaker."""

    __slots__ = ("_breaker", "_inner")

    def __init__(self, inner: Reranker, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    def rerank(self, query: str, passages: list[Passage]) -> list[Passage]:
        """Rerank, or return fusion order unchanged when the breaker is open or rerank fails."""
        if not self._breaker.allow():
            return passages
        try:
            reordered: list[Passage] = self._inner.rerank(query, passages)
        except Exception:
            self._breaker.record_failure()
            return passages
        self._breaker.record_success()
        return reordered


class EmbeddingUnavailableError(RuntimeError):
    """Raised by the breaker-wrapped embedder when its breaker is open. Caught by the retriever."""


__all__ = [
    "BreakerEmbeddingProvider",
    "BreakerReranker",
    "EmbeddingUnavailableError",
]
