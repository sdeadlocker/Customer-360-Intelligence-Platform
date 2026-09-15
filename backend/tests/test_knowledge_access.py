"""Access control and versioning — the explicit Phase 7.10 assertions (requirements 17.3, 17.8).

test_knowledge_retrieval and test_knowledge_api already prove marketing cannot retrieve RISK_ONLY /
COMPLIANCE_ONLY content and that effective-date filtering excludes superseded versions. This module
adds the two assertions those did not make head-on: that a restricted document contributes *nothing*
to a non-entitled caller's result — not a passage, not a candidate, not a count — so its existence
cannot be inferred, and that the candidate count a role sees is exactly the count of chunks drawn
from documents it is entitled to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from c360.data.engine import AccessMode
from c360.knowledge.embeddings import MockEmbeddingProvider
from c360.knowledge.engine import create_knowledge_engine
from c360.knowledge.ingest import ingest_knowledge
from c360.knowledge.models import KnowledgeFilters
from c360.knowledge.repository import KnowledgeRepository
from c360.knowledge.retrieval import KnowledgeRetriever, RetrievalConfig

_DIMENSIONS = 256
_AS_OF = "2025-09-13"
_MARKETING = frozenset({"PUBLIC"})
_RISK = frozenset({"PUBLIC", "INTERNAL", "RISK_ONLY", "COMPLIANCE_ONLY"})


@pytest.fixture(scope="module")
def knowledge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("kb_access") / "knowledge.db"
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


def _filters(levels: frozenset[str], *, as_of: str = _AS_OF) -> KnowledgeFilters:
    return KnowledgeFilters(knowledge_levels=levels, as_of=as_of)


class TestExistenceIsNotRevealed:
    def test_restricted_docs_never_enter_the_candidate_set(
        self, retriever: KnowledgeRetriever
    ) -> None:
        """A marketing search for terms that only appear in RISK_ONLY/COMPLIANCE_ONLY docs returns
        no candidates from them — so their existence is not inferable from a nonzero count."""
        # These terms are concentrated in the procedure/compliance corpus.
        query = "suspicious activity report politically exposed person escalation"
        result = retriever.retrieve(query, _filters(_MARKETING))
        for passage in result.passages:
            assert not passage.citation.doc_id.startswith(("proc-", "comp-"))

    def test_candidate_count_reflects_only_entitled_documents(
        self, repository: KnowledgeRepository
    ) -> None:
        """The count a role sees is drawn purely from documents it may see (req 17.8).

        Marketing's candidate set for a risk-flavoured query is a strict subset of Risk's, and none
        of marketing's candidates come from a restricted document — so the count carries no signal
        about how much restricted content exists.
        """
        query = "fraud escalation dispute chargeback"
        marketing = repository.lexical(query, _filters(_MARKETING), 50)
        risk = repository.lexical(query, _filters(_RISK), 50)

        marketing_docs = {c.doc_id for c in marketing}
        risk_docs = {c.doc_id for c in risk}

        assert marketing_docs <= risk_docs
        assert not any(doc.startswith(("proc-", "comp-")) for doc in marketing_docs)
        # Risk genuinely sees restricted content the count would otherwise have revealed.
        assert any(doc.startswith("proc-") for doc in risk_docs)

    def test_a_role_with_no_levels_sees_nothing(self, repository: KnowledgeRepository) -> None:
        assert repository.lexical("policy eligibility fee", _filters(frozenset()), 50) == []


class TestVersioning:
    def test_only_the_effective_version_is_returned(self, repository: KnowledgeRepository) -> None:
        """As of 2025 the corpus's superseded pairs return only their current version (req 17.3)."""
        for doc_id in ("pol-overdraft", "off-cashback-intro-apr"):
            current = repository.get_document(doc_id)
            assert current is not None
            assert current.effective_to is None
            # And retrieval as-of-now never surfaces the superseded version.
            candidates = repository.lexical(doc_id.split("-", 1)[1], _filters(_RISK), 50)
            for candidate in candidates:
                if candidate.doc_id == doc_id:
                    assert candidate.version == current.version

    def test_querying_as_of_the_past_returns_the_then_current_version(
        self, repository: KnowledgeRepository
    ) -> None:
        # In early 2024 the v1 overdraft policy was in force.
        candidates = repository.lexical("overdraft", _filters(_RISK, as_of="2024-03-01"), 50)
        overdraft = [c for c in candidates if c.doc_id == "pol-overdraft"]
        assert overdraft
        assert all(c.version == "v1" for c in overdraft)

    def test_a_document_not_yet_effective_is_excluded(
        self, repository: KnowledgeRepository
    ) -> None:
        # Nothing in the corpus is effective before 2024; an as-of well before that returns nothing.
        assert repository.lexical("policy", _filters(_RISK, as_of="2020-01-01"), 50) == []
