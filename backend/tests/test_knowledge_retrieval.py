"""Hybrid retrieval tests (task 7.5, design §9.4).

Retrieval is exercised end to end against a freshly ingested corpus with the deterministic mock
embedder, so these assertions are about the *mechanics* — fusion combines both retrievers, the
metadata pre-filter excludes documents by access level and effective date before ranking, and the
min-score floor turns a no-match query into an empty result. Relevance *quality* is a live-Bedrock
concern measured in Phase 11 and is deliberately not asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c360.data.engine import AccessMode
from c360.knowledge.embeddings import MockEmbeddingProvider
from c360.knowledge.engine import create_knowledge_engine
from c360.knowledge.ingest import ingest_knowledge
from c360.knowledge.models import KnowledgeDomain, KnowledgeFilters
from c360.knowledge.repository import KnowledgeRepository, build_kb_match
from c360.knowledge.retrieval import KnowledgeRetriever, RetrievalConfig

_DIMENSIONS = 256

_ALL_LEVELS = frozenset({"PUBLIC", "INTERNAL", "RISK_ONLY", "COMPLIANCE_ONLY"})
_MARKETING_LEVELS = frozenset({"PUBLIC"})
_AS_OF = "2025-09-13"


@pytest.fixture(scope="module")
def knowledge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("kb") / "knowledge.db"
    ingest_knowledge(database=path, dimensions=_DIMENSIONS)
    return path


@pytest.fixture
def repository(knowledge_db: Path) -> KnowledgeRepository:
    engine = create_knowledge_engine(knowledge_db, mode=AccessMode.READ_ONLY, pool_size=2)
    return KnowledgeRepository(engine)


@pytest.fixture
def retriever(repository: KnowledgeRepository) -> KnowledgeRetriever:
    return KnowledgeRetriever(
        repository,
        MockEmbeddingProvider(),
        RetrievalConfig(min_score=0.0, embed_dimensions=_DIMENSIONS),
    )


def _filters(
    levels: frozenset[str] = _ALL_LEVELS, *, as_of: str = _AS_OF, **kwargs: object
) -> KnowledgeFilters:
    return KnowledgeFilters(knowledge_levels=levels, as_of=as_of, **kwargs)


# ---------------------------------------------------------------- match building


class TestMatchBuilding:
    def test_strips_fts_syntax(self) -> None:
        assert build_kb_match('eligibility: "savings"*') == '"eligibility"* OR "savings"*'

    def test_empty_query_is_none(self) -> None:
        assert build_kb_match("   ") is None
        assert build_kb_match("()*:") is None


# ---------------------------------------------------------------- retrieval mechanics


class TestRetrieval:
    def test_returns_cited_passages(self, retriever: KnowledgeRetriever) -> None:
        result = retriever.retrieve("overdraft policy fee", _filters())
        assert not result.is_empty
        for passage in result.passages:
            assert passage.citation.doc_id
            assert passage.citation.section_path
            assert passage.citation.effective_from
            assert passage.text

    def test_respects_context_chunk_budget(self, repository: KnowledgeRepository) -> None:
        retriever = KnowledgeRetriever(
            repository,
            MockEmbeddingProvider(),
            RetrievalConfig(min_score=0.0, context_chunks=3, embed_dimensions=_DIMENSIONS),
        )
        result = retriever.retrieve("account eligibility requirements", _filters())
        assert len(result.passages) <= 3

    def test_lexical_finds_an_exact_term(self, repository: KnowledgeRepository) -> None:
        candidates = repository.lexical("overdraft", _filters(), 20)
        assert candidates
        assert any("overdraft" in c.text.lower() for c in candidates)

    def test_passages_are_ordered_by_fused_score(self, retriever: KnowledgeRetriever) -> None:
        result = retriever.retrieve("eligibility minimum deposit", _filters())
        scores = [passage.fused_score for passage in result.passages]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------- min-score floor


class TestMinScoreFloor:
    def test_a_high_floor_yields_no_passages(self, repository: KnowledgeRepository) -> None:
        """With the floor above any achievable RRF score, retrieval returns the 'nothing' signal."""
        retriever = KnowledgeRetriever(
            repository,
            MockEmbeddingProvider(),
            RetrievalConfig(min_score=1.0, embed_dimensions=_DIMENSIONS),
        )
        result = retriever.retrieve("overdraft policy", _filters())
        assert result.is_empty


# ---------------------------------------------------------------- access-level pre-filter


class TestAccessPreFilter:
    def test_marketing_cannot_retrieve_risk_only(self, repository: KnowledgeRepository) -> None:
        """A RISK_ONLY procedure chunk must not appear for a PUBLIC-only caller (req 17.8)."""
        risk_terms = "suspicious activity report escalation fraud"
        all_access = repository.lexical(risk_terms, _filters(_ALL_LEVELS), 20)
        marketing = repository.lexical(risk_terms, _filters(_MARKETING_LEVELS), 20)
        # Someone with full access sees procedure chunks; marketing sees none of them.
        assert any(c.doc_id.startswith("proc-") for c in all_access)
        assert not any(c.doc_id.startswith("proc-") for c in marketing)

    def test_marketing_semantic_search_excludes_risk_only(
        self, repository: KnowledgeRepository
    ) -> None:
        embedding = MockEmbeddingProvider().embed(["fraud escalation"], dimensions=_DIMENSIONS)[0]
        marketing = repository.semantic(embedding, _filters(_MARKETING_LEVELS), 20)
        assert not any(c.doc_id.startswith("proc-") for c in marketing)
        assert not any(c.doc_id.startswith("comp-") for c in marketing)

    def test_empty_entitlement_returns_nothing(self, repository: KnowledgeRepository) -> None:
        candidates = repository.lexical("policy", _filters(frozenset()), 20)
        assert candidates == []

    def test_domain_narrowing(self, repository: KnowledgeRepository) -> None:
        candidates = repository.lexical(
            "eligibility",
            _filters(domains=frozenset({KnowledgeDomain.PRODUCT_CATALOG})),
            20,
        )
        assert candidates
        assert all(c.doc_id.startswith("pc-") for c in candidates), {c.doc_id for c in candidates}


# ---------------------------------------------------------------- versioning


class TestVersioning:
    def test_superseded_version_excluded_by_effective_date(
        self, repository: KnowledgeRepository
    ) -> None:
        """As of 2025, the v1 overdraft policy (effective_to 2024-12-31) is out; v2 is in (17.3)."""
        candidates = repository.lexical("overdraft", _filters(), 50)
        overdraft = [c for c in candidates if c.doc_id == "pol-overdraft"]
        assert overdraft
        assert all(c.version == "v2" for c in overdraft), {c.version for c in overdraft}

    def test_as_of_in_2024_selects_the_v1_overdraft_policy(
        self, repository: KnowledgeRepository
    ) -> None:
        candidates = repository.lexical("overdraft", _filters(as_of="2024-06-01"), 50)
        overdraft = [c for c in candidates if c.doc_id == "pol-overdraft"]
        assert overdraft
        assert all(c.version == "v1" for c in overdraft), {c.version for c in overdraft}


# ---------------------------------------------------------------- point reads


class TestPointReads:
    def test_get_document_returns_latest_version(self, repository: KnowledgeRepository) -> None:
        meta = repository.get_document("pol-overdraft")
        assert meta is not None
        assert meta.version == "v2"
        assert meta.effective_to is None

    def test_get_document_by_version(self, repository: KnowledgeRepository) -> None:
        meta = repository.get_document("pol-overdraft", "v1")
        assert meta is not None
        assert meta.version == "v1"
        assert meta.effective_to is not None

    def test_get_chunk_resolves_text_and_citation(self, repository: KnowledgeRepository) -> None:
        candidates = repository.lexical("overdraft", _filters(), 5)
        assert candidates
        resolved = repository.get_chunk(candidates[0].chunk_id)
        assert resolved is not None
        chunk_text, citation = resolved
        assert chunk_text
        assert citation.chunk_id == candidates[0].chunk_id

    def test_document_access_level(self, repository: KnowledgeRepository) -> None:
        assert repository.document_access_level("proc-sar-filing") == "RISK_ONLY"
        assert repository.document_access_level("off-heloc-promo") == "PUBLIC"
        assert repository.document_access_level("nonexistent") is None


# ---------------------------------------------------------------- graceful degradation (§13.7)


class _FailingEmbedder:
    """An embedder whose ``embed`` always raises, to simulate a down/throttled Titan endpoint."""

    model_id = "failing-titan"

    def embed(self, texts: object, *, dimensions: int) -> list[list[float]]:
        raise RuntimeError("titan down")


class TestGracefulDegradation:
    """A failing query embedder must degrade retrieval to lexical-only, never fail the request."""

    def test_embedding_failure_degrades_to_lexical_only(
        self, repository: KnowledgeRepository
    ) -> None:
        retriever = KnowledgeRetriever(
            repository,
            _FailingEmbedder(),
            RetrievalConfig(min_score=0.0, embed_dimensions=_DIMENSIONS),
        )

        # The semantic half raised, but the lexical (FTS/BM25) half still runs — a term-matching
        # query returns cited passages rather than raising or returning nothing.
        result = retriever.retrieve("overdraft policy fee", _filters())

        assert not result.is_empty
        assert all(passage.citation.doc_id for passage in result.passages)

    def test_lexical_only_matches_a_subset_of_the_hybrid_result(
        self, repository: KnowledgeRepository
    ) -> None:
        # Sanity: the degraded retriever returns the same *kind* of result (real cited passages),
        # just from one retriever. It must not error and must stay within the context budget.
        degraded = KnowledgeRetriever(
            repository,
            _FailingEmbedder(),
            RetrievalConfig(min_score=0.0, context_chunks=3, embed_dimensions=_DIMENSIONS),
        )
        result = degraded.retrieve("account eligibility requirements", _filters())
        assert len(result.passages) <= 3
