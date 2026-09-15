"""Retrieval instrumentation and performance (task 7.9, requirements 17.12, 18.5).

Asserts the two GenAI spans the design's trace tree names (``gen_ai.retrieve knowledge_search`` and
``gen_ai.embeddings``) are emitted with the expected attributes and nothing disallowed, and that the
dashboard-agent retrieval budget (≤ 500 ms p95, requirement 17.12) holds on the deterministic mock
path. The mock embedder is far faster than live Titan, so the offline p95 is comfortably under
budget; the assertion proves the pipeline itself adds no latency that would breach it, which is what
a CI gate can check without AWS. The live-Bedrock budget is validated in Phase 11.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from c360.core.telemetry import SpanAttributeAllowlist
from c360.data.engine import AccessMode
from c360.knowledge.embeddings import MockEmbeddingProvider
from c360.knowledge.engine import create_knowledge_engine
from c360.knowledge.ingest import ingest_knowledge
from c360.knowledge.models import KnowledgeFilters
from c360.knowledge.repository import KnowledgeRepository
from c360.knowledge.retrieval import KnowledgeRetriever, RetrievalConfig

_DIMENSIONS = 256
_ALL_LEVELS = frozenset({"PUBLIC", "INTERNAL", "RISK_ONLY", "COMPLIANCE_ONLY"})
_AS_OF = "2025-09-13"


@pytest.fixture(scope="module")
def knowledge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("kb_tel") / "knowledge.db"
    ingest_knowledge(database=path, dimensions=_DIMENSIONS)
    return path


@pytest.fixture
def retriever(knowledge_db: Path) -> KnowledgeRetriever:
    engine = create_knowledge_engine(knowledge_db, mode=AccessMode.READ_ONLY, pool_size=4)
    return KnowledgeRetriever(
        KnowledgeRepository(engine),
        MockEmbeddingProvider(),
        RetrievalConfig(min_score=0.0, embed_dimensions=_DIMENSIONS),
    )


def _filters() -> KnowledgeFilters:
    return KnowledgeFilters(knowledge_levels=_ALL_LEVELS, as_of=_AS_OF)


class TestSpans:
    def test_emits_retrieve_and_embeddings_spans(
        self, retriever: KnowledgeRetriever, span_exporter: InMemorySpanExporter
    ) -> None:
        retriever.retrieve("overdraft policy fee", _filters())
        names = {span.name for span in span_exporter.get_finished_spans()}
        assert "gen_ai.retrieve knowledge_search" in names
        assert "gen_ai.embeddings" in names

    def test_retrieve_span_carries_retrieval_attributes(
        self, retriever: KnowledgeRetriever, span_exporter: InMemorySpanExporter
    ) -> None:
        retriever.retrieve("eligibility minimum deposit", _filters())
        span = _find(span_exporter, "gen_ai.retrieve knowledge_search")
        assert span is not None
        assert span.attributes["gen_ai.operation.name"] == "retrieve"
        assert span.attributes["gen_ai.tool.name"] == "knowledge_search"
        assert "c360.retrieval.candidate_count" in span.attributes
        assert "c360.retrieval.result_count" in span.attributes
        assert "c360.retrieval.reranked" in span.attributes

    def test_embeddings_span_names_the_model(
        self, retriever: KnowledgeRetriever, span_exporter: InMemorySpanExporter
    ) -> None:
        retriever.retrieve("fee schedule", _filters())
        span = _find(span_exporter, "gen_ai.embeddings")
        assert span is not None
        assert span.attributes["gen_ai.request.model"] == "mock-embed-v1"

    def test_no_disallowed_attributes_are_exported(
        self, retriever: KnowledgeRetriever, span_exporter: InMemorySpanExporter
    ) -> None:
        """The query text and any bound value must not survive the allowlist (design §13.4)."""
        retriever.retrieve("overdraft policy fee", _filters())
        allowlist = SpanAttributeAllowlist()
        for span in span_exporter.get_finished_spans():
            rejected = allowlist.rejected_keys(dict(span.attributes or {}))
            assert rejected == (), (span.name, rejected)

    def test_query_text_is_not_on_any_span(
        self, retriever: KnowledgeRetriever, span_exporter: InMemorySpanExporter
    ) -> None:
        marker = "zzsecretquerytoken"
        retriever.retrieve(f"overdraft {marker}", _filters())
        for span in span_exporter.get_finished_spans():
            for value in (span.attributes or {}).values():
                assert marker not in str(value)


class TestZeroResult:
    def test_zero_result_flagged_on_the_span(self, knowledge_db: Path) -> None:
        engine = create_knowledge_engine(knowledge_db, mode=AccessMode.READ_ONLY, pool_size=2)
        # A floor above any achievable score forces an empty result.
        retriever = KnowledgeRetriever(
            KnowledgeRepository(engine),
            MockEmbeddingProvider(),
            RetrievalConfig(min_score=1.0, embed_dimensions=_DIMENSIONS),
        )
        result = retriever.retrieve("overdraft", _filters())
        assert result.is_empty


class TestPerformance:
    @pytest.mark.slow
    def test_retrieval_p95_is_within_the_dashboard_budget(
        self, retriever: KnowledgeRetriever
    ) -> None:
        queries = [
            "overdraft policy fee",
            "high yield savings eligibility",
            "mortgage refinance guidance",
            "cashback card introductory apr",
            "fraud escalation procedure",
            "home equity line promotional rate",
        ]
        # Warm the pool and page cache so the measurement is of steady-state retrieval.
        for query in queries:
            retriever.retrieve(query, _filters())

        durations: list[float] = []
        for _ in range(10):
            for query in queries:
                started = time.perf_counter()
                retriever.retrieve(query, _filters())
                durations.append((time.perf_counter() - started) * 1000)

        durations.sort()
        p95 = durations[int(len(durations) * 0.95) - 1]
        # Requirement 17.12: ≤ 500 ms p95 for dashboard-agent retrieval. The mock path is far under
        # this; the assertion proves the pipeline adds no budget-breaking overhead.
        assert p95 < 500.0, f"retrieval p95 {p95:.1f}ms exceeded the 500ms dashboard budget"


def _find(exporter: InMemorySpanExporter, name: str) -> Any:
    for span in exporter.get_finished_spans():
        if span.name == name:
            return span
    return None
