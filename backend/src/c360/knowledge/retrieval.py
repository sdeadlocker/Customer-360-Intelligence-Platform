"""The retrieval pipeline — fusion, floor, assembly (task 7.5; design §9.4).

:class:`KnowledgeRetriever` is the orchestration the design §9.4 diagram describes, minus the two
steps that live in their own modules for clarity: query redaction (task 7.7,
:mod:`c360.knowledge.redaction`) happens before the retriever is called, and reranking (task 7.6,
:mod:`c360.knowledge.rerank`) is an optional stage the retriever invokes. What is here is the fusion
core:

1. embed the (already-redacted) query;
2. run the lexical and semantic retrievers over the SQL pre-filtered set (the repository);
3. fuse the two ranked lists with Reciprocal Rank Fusion, ``score = Σ 1/(k + rank)`` with ``k = 60``
   (design §9.4 step 5) — chosen because it needs no calibration between BM25 and cosine scales;
4. drop anything below the minimum-score floor, so a weak match returns nothing rather than noise
   (requirement 17.10, the "no supporting guidance found" behaviour);
5. assemble the top-N passages within the context-chunk budget, each carrying a resolvable citation.

The retriever does **not** wrap passages in the untrusted-reference block — that is the prompt
layer's job (task 7.7 / Phase 8), applied when passages are placed into a prompt, not when they are
returned to a REST caller who renders them as citations. Keeping containment at the prompt boundary
means the ``knowledge_search`` tool can return clean passage text for the UI while the agent path
still gets the delimited, instruction-inert block.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from c360.core.logging import get_logger
from c360.core.telemetry import SpanAttr, get_tracer
from c360.knowledge.embeddings import EmbeddingProvider
from c360.knowledge.metrics import RetrievalMetrics
from c360.knowledge.models import (
    Candidate,
    KnowledgeFilters,
    KnowledgeSearchResult,
    Passage,
    SectionRef,
)
from c360.knowledge.redaction import redact_query
from c360.knowledge.repository import KnowledgeRepository
from c360.knowledge.rerank import NullReranker, Reranker

_logger = get_logger(__name__)

_TRACER_NAME = "c360.knowledge.retrieval"

#: GenAI semantic-convention span names (design §13.2 trace tree).
_RETRIEVE_SPAN = "gen_ai.retrieve knowledge_search"
_EMBED_SPAN = "gen_ai.embeddings"


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """The tunables the retriever reads, lifted straight from :class:`~c360.core.config.Settings`.

    Held as a small frozen record rather than the whole settings object so the retriever's
    dependencies are explicit and a test can construct one without a full settings instance.
    """

    lexical_k: int = 20
    semantic_k: int = 20
    rrf_k: int = 60
    context_chunks: int = 5
    min_score: float = 0.02
    embed_dimensions: int = 1024


class KnowledgeRetriever:
    """Runs the fusion pipeline over a :class:`KnowledgeRepository` (task 7.5, design §9.4)."""

    __slots__ = ("_config", "_embedder", "_metrics", "_repository", "_reranker")

    def __init__(
        self,
        repository: KnowledgeRepository,
        embedder: EmbeddingProvider,
        config: RetrievalConfig,
        reranker: Reranker | None = None,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._config = config
        self._reranker = reranker if reranker is not None else NullReranker()
        self._metrics = RetrievalMetrics()

    def retrieve(
        self, query: str, filters: KnowledgeFilters, *, rerank: bool = False
    ) -> KnowledgeSearchResult:
        """Retrieve the top passages for ``query`` under ``filters``.

        The query is redacted here (task 7.7, requirement 17.9) before it is embedded or matched, so
        an identifier that slips into a raw query never reaches the embedding model or the index —
        redaction at the pipeline entry means no caller can bypass it. A caller that has already
        built a clean query from qualifiers (``build_retrieval_query``) loses nothing: redaction of
        an already-clean query is a no-op. Returns an empty result — the "no supporting guidance
        found" signal — when nothing clears the relevance floor.

        ``rerank`` requests the optional Bedrock rerank stage (design §9.4 step 6). It is opt-in per
        call because the design scopes reranking to Q&A only — the dashboard-agent path leaves it
        ``False`` so it never pays the round trip its 500 ms budget cannot absorb. Reranking runs
        *after* the floor and budget are applied, reordering the final passages, and degrades to
        fusion order if the reranker is unavailable (requirement 17.13). ``reranked`` on the result
        reports whether a real reordering model actually ran.
        """
        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(_RETRIEVE_SPAN) as span:
            span.set_attribute("gen_ai.operation.name", "retrieve")
            span.set_attribute("gen_ai.tool.name", "knowledge_search")

            redacted = redact_query(query)

            lexical = self._timed(
                "lexical",
                lambda: self._repository.lexical(redacted, filters, self._config.lexical_k),
            )
            # Embedding circuit breaker (task 8.11, requirement 13.7): if the embedder is
            # unavailable, `_embed` returns None and the semantic half is skipped — retrieval
            # degrades to lexical-only rather than failing. Fusion over one list is that list.
            embedding = self._embed(redacted)
            semantic = (
                []
                if embedding is None
                else self._timed(
                    "semantic",
                    lambda: self._repository.semantic(embedding, filters, self._config.semantic_k),
                )
            )

            fused = self._timed("fusion", lambda: self._fuse(lexical, semantic))
            candidate_count = len({c.chunk_id for c in (*lexical, *semantic)})
            passages = self._timed("assembly", lambda: self._assemble(fused))

            reranked = False
            if rerank and passages and not isinstance(self._reranker, NullReranker):
                passages = self._timed("rerank", lambda: self._reranker.rerank(query, passages))
                reranked = True

            span.set_attribute(SpanAttr.RETRIEVAL_CANDIDATES, candidate_count)
            span.set_attribute(SpanAttr.RETRIEVAL_RESULTS, len(passages))
            span.set_attribute(SpanAttr.RETRIEVAL_RERANKED, reranked)
            span.set_attribute(SpanAttr.RETRIEVAL_ZERO_RESULT, len(passages) == 0)
            span.set_attribute(SpanAttr.OUTCOME, "ok")

            self._metrics.record_outcome(
                candidate_count=candidate_count,
                result_count=len(passages),
                reranked=reranked,
            )
            return KnowledgeSearchResult(
                passages=tuple(passages),
                candidate_count=candidate_count,
                reranked=reranked,
            )

    def _embed(self, query: str) -> list[float] | None:
        """Embed the query inside a ``gen_ai.embeddings`` span, or ``None`` if unavailable (§13.7).

        A ``None`` return is the embedding-breaker signal: the caller skips the semantic stage and
        the search degrades to lexical-only. An embedder raising (throttling, an open breaker in a
        breaker-wrapped embedder, boto3 missing) is caught here so retrieval never fails *because*
        embedding was unavailable — the same "optional stage must not fail the request" discipline
        the reranker already applies.
        """
        tracer = get_tracer(_TRACER_NAME)
        started = time.perf_counter()
        with tracer.start_as_current_span(_EMBED_SPAN) as span:
            span.set_attribute("gen_ai.operation.name", "embeddings")
            span.set_attribute("gen_ai.request.model", self._embedder.model_id)
            try:
                vector = self._embedder.embed([query], dimensions=self._config.embed_dimensions)[0]
            except Exception:
                span.set_attribute(SpanAttr.OUTCOME, "error")
                self._metrics.record_stage("embedding", (time.perf_counter() - started) * 1000)
                return None
            span.set_attribute(SpanAttr.OUTCOME, "ok")
        self._metrics.record_stage("embedding", (time.perf_counter() - started) * 1000)
        return vector

    def _timed[T](self, stage: str, work: Callable[[], T]) -> T:
        """Run ``work``, recording its wall-clock duration against ``stage`` in the metrics."""
        started = time.perf_counter()
        result = work()
        self._metrics.record_stage(stage, (time.perf_counter() - started) * 1000)
        return result

    # ---------------------------------------------------------------- fusion
    def _fuse(
        self, lexical: list[Candidate], semantic: list[Candidate]
    ) -> list[tuple[Candidate, float]]:
        """Reciprocal Rank Fusion over the two ranked lists (design §9.4 step 5).

        ``score = Σ 1/(k + rank)`` summed across the retrievers a chunk appears in. A chunk found by
        both retrievers therefore outranks one found by only one, which is the whole point of hybrid
        retrieval. Ties break on chunk id so the order is total and the result is deterministic.
        """
        k = self._config.rrf_k
        scores: dict[str, float] = {}
        best: dict[str, Candidate] = {}
        for candidate in (*lexical, *semantic):
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (
                k + candidate.rank
            )
            # Keep the lowest-rank (best) sighting of the chunk as its canonical candidate, so the
            # assembled passage carries the text either retriever returned (they are identical).
            existing = best.get(candidate.chunk_id)
            if existing is None or candidate.rank < existing.rank:
                best[candidate.chunk_id] = candidate
        ordered = sorted(best.values(), key=lambda c: (-scores[c.chunk_id], c.chunk_id))
        return [(candidate, scores[candidate.chunk_id]) for candidate in ordered]

    # ---------------------------------------------------------------- assembly
    def _assemble(self, fused: list[tuple[Candidate, float]]) -> list[Passage]:
        """Apply the min-score floor and take the top ``context_chunks`` as cited passages.

        The floor (design §9.4, ``RETRIEVAL_MIN_SCORE``) is what turns "the best of a bad lot" into
        "nothing found": a candidate whose fused score is below it is dropped, so a query with no
        genuinely relevant chunk returns an empty result rather than the least-irrelevant one.
        """
        passages: list[Passage] = []
        for candidate, score in fused:
            if score < self._config.min_score:
                continue
            passages.append(
                Passage(
                    text=candidate.text,
                    fused_score=score,
                    citation=self._citation(candidate),
                )
            )
            if len(passages) >= self._config.context_chunks:
                break
        return passages

    def _citation(self, candidate: Candidate) -> SectionRef:
        """Build a resolvable citation for a candidate, reading the document header for the title.

        A citation needs the document title and effective dates the candidate does not carry, so it
        resolves them from ``kb_document`` via the repository. Cheap: the document is already in the
        page cache from the pre-filter join.
        """
        meta = self._repository.get_document(candidate.doc_id, candidate.version)
        title = meta.title if meta is not None else candidate.doc_id
        effective_from = meta.effective_from if meta is not None else ""
        effective_to = meta.effective_to if meta is not None else None
        return SectionRef(
            chunk_id=candidate.chunk_id,
            doc_id=candidate.doc_id,
            version=candidate.version,
            section_path=candidate.section_path,
            title=title,
            effective_from=effective_from,
            effective_to=effective_to,
        )


__all__ = ["KnowledgeRetriever", "RetrievalConfig"]
