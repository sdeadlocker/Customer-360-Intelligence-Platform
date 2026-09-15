"""Task 8.11 — circuit breakers and their fallbacks (design §13.7).

Covers the breaker state machine (closed -> open on threshold -> half-open after cool-off ->
closed on a trial success / re-open on a trial failure), and each of the three fallbacks: the
generation breaker routes an agent to the deterministic template (degraded), the embedding breaker
makes its wrapper raise (which the retriever turns into lexical-only), and the rerank breaker
returns fusion order unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib

from c360.agents.breaker import BreakerState, CircuitBreaker
from c360.agents.degrade import (
    BreakerEmbeddingProvider,
    BreakerReranker,
    EmbeddingUnavailableError,
)
from c360.agents.provider import CompletionRequest, CompletionResponse
from c360.agents.redaction import PromptRedactor
from c360.agents.runner import AgentInput, AgentRunner
from c360.agents.specs import AGENT_SPECS
from c360.tools.facts import FactTable


# ---------------------------------------------------------------- state machine
def test_breaker_opens_after_threshold():
    breaker = CircuitBreaker("x", threshold=3, cooloff_s=100)
    assert breaker.state is BreakerState.CLOSED
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.allow() is False


def test_success_resets_failure_count():
    breaker = CircuitBreaker("x", threshold=3, cooloff_s=100)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED  # count reset, not yet at threshold


def test_open_transitions_to_half_open_after_cooloff(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("c360.agents.breaker.time.monotonic", lambda: clock["now"])
    breaker = CircuitBreaker("x", threshold=1, cooloff_s=10)
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock["now"] = 11
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow() is True


def test_half_open_success_closes(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("c360.agents.breaker.time.monotonic", lambda: clock["now"])
    breaker = CircuitBreaker("x", threshold=1, cooloff_s=10)
    breaker.record_failure()
    clock["now"] = 11
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_half_open_failure_reopens(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("c360.agents.breaker.time.monotonic", lambda: clock["now"])
    breaker = CircuitBreaker("x", threshold=1, cooloff_s=10)
    breaker.record_failure()
    clock["now"] = 11
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN


# ---------------------------------------------------------------- generation fallback
class _FailingProvider:
    model_id = "failing-llm"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise RuntimeError("bedrock down")

    async def stream(self, request) -> object:  # pragma: no cover - not used here
        raise RuntimeError

    async def embed(self, texts, *, dimensions) -> object:  # pragma: no cover - not used here
        raise RuntimeError


def _item() -> AgentInput:
    spec = AGENT_SPECS["financial_health"].with_allowlist(frozenset({"net_worth_cents"}))
    facts = FactTable.builder()
    facts.add(entity_type="financial_profile", entity_id="C1", field="net_worth_cents", value=100)
    return AgentInput(
        spec=spec,
        facts=facts.build(),
        passages=(),
        prompt_body="be grounded",
        prompt_version="v1+abc",
    )


def test_generation_failure_degrades_to_template_and_trips_breaker():
    breaker = CircuitBreaker("generation", threshold=1, cooloff_s=100)
    runner = AgentRunner(_FailingProvider(), PromptRedactor(), breaker=breaker)
    result = asyncio.run(runner.run(_item()))
    # Degraded template render, and the breaker tripped from the failure.
    assert result.degraded is True
    assert result.outputs.narrative  # the template still produced an accurate card
    assert breaker.state is BreakerState.OPEN


def test_open_generation_breaker_skips_the_provider():
    breaker = CircuitBreaker("generation", threshold=1, cooloff_s=100)
    breaker.record_failure()  # force open
    assert breaker.allow() is False
    runner = AgentRunner(_FailingProvider(), PromptRedactor(), breaker=breaker)
    # Even though the provider would raise, an open breaker means it is never called; degraded card.
    result = asyncio.run(runner.run(_item()))
    assert result.degraded is True


# ---------------------------------------------------------------- embedding fallback
class _Embedder:
    model_id = "e"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts, *, dimensions) -> list[list[float]]:
        self.calls += 1
        return [[0.0] * dimensions for _ in texts]


def test_embedding_breaker_open_raises_to_force_lexical_only():
    breaker = CircuitBreaker("embedding", threshold=1, cooloff_s=100)
    breaker.record_failure()
    wrapped = BreakerEmbeddingProvider(_Embedder(), breaker)
    try:
        wrapped.embed(["q"], dimensions=8)
    except EmbeddingUnavailableError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected EmbeddingUnavailableError when breaker is open")


def test_embedding_breaker_records_failure_and_reraises():
    class _Boom:
        model_id = "e"

        def embed(self, texts, *, dimensions) -> list[list[float]]:
            raise RuntimeError("titan down")

    breaker = CircuitBreaker("embedding", threshold=1, cooloff_s=100)
    wrapped = BreakerEmbeddingProvider(_Boom(), breaker)
    with contextlib.suppress(RuntimeError):
        wrapped.embed(["q"], dimensions=8)
    assert breaker.state is BreakerState.OPEN


def test_embedding_wrapper_passes_through_when_closed():
    embedder = _Embedder()
    wrapped = BreakerEmbeddingProvider(embedder, CircuitBreaker("embedding"))
    vectors = wrapped.embed(["a", "b"], dimensions=4)
    assert len(vectors) == 2
    assert embedder.calls == 1


# ---------------------------------------------------------------- rerank fallback
class _Passages(list):
    pass


def test_rerank_breaker_open_returns_fusion_order():
    breaker = CircuitBreaker("rerank", threshold=1, cooloff_s=100)
    breaker.record_failure()

    class _Reranker:
        def rerank(self, query, passages) -> list:  # pragma: no cover - must not be called
            raise AssertionError("reranker called while breaker open")

    wrapped = BreakerReranker(_Reranker(), breaker)
    passages = ["p1", "p2"]
    assert wrapped.rerank("q", passages) is passages


def test_rerank_failure_falls_back_to_fusion_order():
    class _Reranker:
        def rerank(self, query, passages) -> list:
            raise RuntimeError("rerank down")

    breaker = CircuitBreaker("rerank", threshold=1, cooloff_s=100)
    wrapped = BreakerReranker(_Reranker(), breaker)
    passages = ["p1", "p2"]
    assert wrapped.rerank("q", passages) == passages
    assert breaker.state is BreakerState.OPEN
