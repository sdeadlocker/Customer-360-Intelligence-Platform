"""The ``LLMProvider`` port and the ``MockLLMProvider`` (task 8.1, design §8.5).

The provider seam is the single boundary between the agent graph and *any* model. Above it, an agent
assembles a :class:`CompletionRequest` — a system instruction, a fenced fact table, an optional
fenced block of untrusted reference passages, and a task instruction — and receives a
:class:`CompletionResponse`. Below it, exactly one implementation runs: the mock under
``LLM_PROVIDER=mock`` (this module) or Bedrock otherwise (:mod:`c360.agents.bedrock`), selected by
:func:`build_llm_provider`, which mirrors the embedding selector so a deployment can never end up
with a mock generator and a live embedder or vice versa.

Why the mock renders from the fact table rather than returning a canned string
------------------------------------------------------------------------------

The phase gate requires the *same agent suite* to pass with the mock and with live Bedrock. That is
only meaningful if the mock's output is a real function of the agent's inputs: it must cite the fact
it was given, and it must never invent a figure — because the claim validator (task 8.5) runs on the
mock's output too. So the mock is a deterministic Jinja renderer over the fact table. It produces a
narrative whose every number is copied from a fact and tagged with that fact's id, which is exactly
the shape a well-behaved live model produces and the shape the validator accepts. The pseudo-random
model does not enter here at all — determinism is the whole point (design §8.5).

Embeddings live on the same port for symmetry with design §8.5, but the concrete embedding work is
still the Phase 7 :class:`~c360.knowledge.embeddings.EmbeddingProvider`; :meth:`embed` delegates to
it so there is one embedding implementation, not two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from c360.knowledge.redaction import wrap_untrusted
from c360.tools.facts import FactTable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from c360.agents.qa_types import ConverseRequest, ConverseResponse
    from c360.core.config import Settings
    from c360.knowledge.embeddings import Vector


# ================================================================ request / response types


class PassageRef(BaseModel):
    """A retrieved passage as an agent hands it to the provider: text plus its citation id.

    The ``passage_id`` (``P1``, ``P2``, ...) is the reference the model is told to cite for advice,
    kept separate from a fact's ``F``-id so the claim validator can tell a figure that resolves to a
    fact from one that resolves only to a passage (design §8.4, rule 4 in §2.2).
    """

    model_config = ConfigDict(frozen=True)

    passage_id: str = Field(pattern=r"^P\d+$")
    text: str
    doc_id: str
    section_path: str


class CompletionRequest(BaseModel):
    """Everything a provider needs to produce one agent narrative.

    ``facts`` is the numbered, provenance-bound table the narrative must draw every figure from.
    ``passages`` is the *separate* block of retrieved guidance, fenced as untrusted reference
    material before it reaches any model (:func:`~c360.knowledge.redaction.wrap_untrusted`) so a
    passage that contains instruction-like text is read as data, never obeyed. ``task`` names the
    structured job (e.g. which output fields to fill); ``max_tokens`` and ``temperature`` are the
    per-agent generation controls design §8.5 lists as cost and latency levers.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    agent: str
    system: str
    task: str
    facts: FactTable
    passages: tuple[PassageRef, ...] = ()
    max_tokens: int = Field(default=1024, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)

    def reference_block(self) -> str:
        """The passages fenced as an instruction-inert untrusted block, or ``""`` when none.

        This is the one place retrieved text is turned into prompt content, so containment
        (requirement 17.11) is applied here rather than trusted to each agent to remember.
        """
        if not self.passages:
            return ""
        return wrap_untrusted([f"[{p.passage_id}] {p.text}" for p in self.passages])


class TokenUsage(BaseModel):
    """Input/output token counts a provider reports, for cost and observability (task 8.10)."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0


class CompletionResponse(BaseModel):
    """A provider's answer: the narrative text, the model that produced it, and token usage.

    ``text`` is the raw narrative the claim validator (task 8.5) parses; ``model_id`` flows onto the
    :class:`AgentResult` and the audit record so a narrative is always attributable to a model, mock
    or otherwise (task 8.10). ``finish_reason`` is advisory.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    model_id: str
    usage: TokenUsage = TokenUsage()
    finish_reason: str = "stop"


# ================================================================ the port


@runtime_checkable
class LLMProvider(Protocol):
    """The seam between the agent graph and any model (design §8.5).

    A single ``model_id`` identifies whatever produced a completion: a real Bedrock model id, or the
    mock sentinel — so it can go in the cache key and the audit record unconditionally.
    """

    model_id: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Produce one narrative for ``request``."""
        ...

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Yield the narrative in chunks. Used by the SSE path (task 8.9)."""
        ...

    async def converse(self, request: ConverseRequest) -> ConverseResponse:
        """One tool-calling turn for the Q&A ReAct loop (task 9.1, design §10.1).

        Given the running conversation and the available tool schemas, return either the tool calls
        the model wants to run or its final answer. The loop, not the provider, decides when to stop
        — the provider only reports what the model asked for this turn.
        """
        ...

    async def embed(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        """Embed ``texts``. Delegates to the Phase 7 embedding provider."""
        ...


# ================================================================ the mock


class MockLLMProvider:
    """Deterministic, fact-grounded narratives for CI, evaluation and the breaker fallback.

    Selected by ``LLM_PROVIDER=mock``. It renders each agent's narrative from a Jinja template bound
    by agent name (:mod:`c360.agents.templates`), fed the request's fact table and passage
    references. The output is deterministic for a given request, cites facts by their ``F``-ids and
    passages by their ``P``-ids, and never contains a figure that is not copied from a fact — so it
    passes the same claim validator the live path must pass. Embeddings delegate to the Phase 7
    :class:`~c360.knowledge.embeddings.MockEmbeddingProvider` so retrieval is offline-testable too.
    """

    #: A recognisable sentinel so a cached narrative or an audit row is traceable to the mock and
    #: never mistaken for a Bedrock model's output.
    model_id = "mock-llm-v1"

    __slots__ = ("_embedder", "_renderer")

    def __init__(self) -> None:
        from c360.agents.templates import TemplateRenderer  # noqa: PLC0415 - avoid import cycle
        from c360.knowledge.embeddings import MockEmbeddingProvider  # noqa: PLC0415

        self._renderer = TemplateRenderer()
        self._embedder = MockEmbeddingProvider()

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        text = self._renderer.render(request)
        # A cheap, deterministic token estimate keeps the usage-derived cost metric exercised on the
        # mock path without pretending to a real tokenizer's counts.
        return CompletionResponse(
            text=text,
            model_id=self.model_id,
            usage=TokenUsage(
                input_tokens=_estimate_tokens(request.system) + _estimate_tokens(request.task),
                output_tokens=_estimate_tokens(text),
            ),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Yield the rendered narrative a line at a time, deterministically."""
        response = await self.complete(request)
        return _line_stream(response.text)

    async def converse(self, request: ConverseRequest) -> ConverseResponse:
        """Deterministically drive the Q&A ReAct loop offline (task 9.1, design §8.5/§10.1).

        The mock is not a language model, so it emulates the *shape* of a tool-using turn rather
        than reasoning: on the first assistant turn it asks for the tools the route and the question
        keywords imply (choosing only from the tools actually offered in the request), and once tool
        results are in the history it renders a final, fact-grounded answer whose every figure is
        copied from a returned fact and cited by its ``F``-id — so it passes the same validator the
        live path must pass. This lets the whole Q&A suite run in CI with no AWS account, exactly as
        the dashboard suite does.
        """
        from c360.agents.qa_mock import mock_converse  # noqa: PLC0415 - avoid import cycle

        return mock_converse(request, model_id=self.model_id)

    async def embed(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        return self._embedder.embed(texts, dimensions=dimensions)


def _estimate_tokens(text: str) -> int:
    """A deterministic, tokenizer-free estimate: roughly four characters to a token."""
    return max(1, len(text) // 4)


def _line_stream(text: str) -> AsyncIterator[str]:
    async def _gen() -> AsyncIterator[str]:
        for line in text.splitlines(keepends=True):
            yield line

    return _gen()


# ================================================================ selector


def build_llm_provider(settings: Settings) -> LLMProvider:
    """Select the generation provider from configuration (task 8.1/8.2).

    Bound to ``LLM_PROVIDER`` exactly as :func:`~c360.knowledge.embeddings.build_embedding_provider`
    is, so the same switch routes generation and embeddings and the two can never diverge. ``mock``
    returns the deterministic provider; anything else constructs the Bedrock provider, whose AWS SDK
    imports are lazy so this function stays importable with no AWS dependency installed.
    """
    from c360.core.config import LlmProvider  # noqa: PLC0415 - avoid import cycle at module load

    if settings.llm_provider is LlmProvider.MOCK:
        return MockLLMProvider()
    from c360.agents.bedrock import BedrockProvider  # noqa: PLC0415 - lazy so the SDK is optional

    return BedrockProvider(settings)


# `facts: FactTable` is a Pydantic field of a frozen model; rebuild so the forward reference is
# resolved at import, mirroring how `c360.tools.facts` rebuilds `FactTable` itself.
CompletionRequest.model_rebuild()


__all__ = [
    "CompletionRequest",
    "CompletionResponse",
    "LLMProvider",
    "MockLLMProvider",
    "PassageRef",
    "TokenUsage",
    "build_llm_provider",
]
