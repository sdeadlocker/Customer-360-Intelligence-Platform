"""Knowledge-layer value types (task 7.2, 7.5; design §9.3, §9.6).

These are the shapes the knowledge repository, the retrieval pipeline and the ``knowledge_search``
tool pass around. They are deliberately separate from the customer :mod:`c360.domain.models`: a
knowledge document carries no customer provenance (``as_of_date`` / ``source_system``) because it is
institutional content, not a customer record (design §9.1 scope boundary). What it carries instead
is *citation* provenance — document, version, section and effective date — which is what
requirement 17.5 makes sufficient to cite a source.

``Candidate`` is the internal shape the lexical and semantic retrievers return, carrying the raw
per-retriever rank so Reciprocal Rank Fusion (design §9.4 step 5) can combine them. ``Passage`` is
the cited, assembled shape a tool or answer returns. Keeping them distinct means the fusion math
never leaks into the tool contract and a passage the UI renders never carries a raw BM25 score that
would be meaningless to a user.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeDomain(StrEnum):
    """The six knowledge domains (design §9.2). Mirrors the ``kb_document.domain`` CHECK set."""

    PRODUCT_CATALOG = "product_catalog"
    POLICY = "policy"
    PROCEDURE = "procedure"
    OFFER_TERMS = "offer_terms"
    PLAYBOOK = "playbook"
    COMPLIANCE = "compliance"


class DocumentMeta(BaseModel):
    """Document-level metadata, enough to cite and render a document header (requirement 17.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str
    title: str
    domain: KnowledgeDomain
    version: str
    effective_from: str
    effective_to: str | None = None
    jurisdiction: str | None = None
    access_level: str
    product_code: str | None = None
    business_group: str | None = None
    source_uri: str | None = None


class SectionRef(BaseModel):
    """A resolvable reference to a document section, the target of a knowledge citation.

    Everything a UI needs to open the passage and show where it came from: the document, the version
    that was in force, the section path within it, and the effective date. Requirement 17.6.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    doc_id: str
    version: str
    section_path: str
    title: str
    effective_from: str
    effective_to: str | None = None


class Candidate(BaseModel):
    """One retrieved chunk with its per-retriever rank, before fusion (design §9.4).

    ``rank`` is the 1-based position this candidate held in the retriever that produced it (BM25 or
    cosine). Reciprocal Rank Fusion consumes the rank, not a score, which is exactly why RRF is used
    here: it needs no calibration between the two differently-scaled retrievers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    doc_id: str
    version: str
    section_path: str
    text: str
    rank: int = Field(ge=1)


class Passage(BaseModel):
    """A cited, assembled passage returned by retrieval (requirement 17.5, 17.6).

    ``fused_score`` is the Reciprocal Rank Fusion score (or the rerank score when reranking ran); it
    orders the context block and lets the minimum-score floor drop weak matches. ``citation`` is the
    resolvable section reference the UI opens.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    fused_score: float
    citation: SectionRef


class KnowledgeFilters(BaseModel):
    """The metadata pre-filter applied in SQL before ranking (design §9.4 step 2).

    ``knowledge_levels`` is the caller's entitlement — the pre-filter keeps only documents whose
    ``access_level`` is in this set, so a document above the role is never a candidate and its
    existence is not revealed (requirement 17.8). ``as_of`` bounds the effective-date window so a
    superseded version is excluded (requirement 17.3). ``domains`` and ``product_code`` are optional
    narrowing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Access levels the caller may retrieve. Empty means "nothing is visible", which correctly
    #: returns no candidates rather than everything.
    knowledge_levels: frozenset[str]
    #: ISO date the query is "as of". A document is eligible when effective_from <= as_of and
    #: (effective_to IS NULL OR as_of <= effective_to).
    as_of: str
    domains: frozenset[KnowledgeDomain] = frozenset()
    product_code: str | None = None


class KnowledgeSearchResult(BaseModel):
    """The result of a retrieval call: the assembled passages and how the search behaved.

    ``passages`` is the ordered, token-budgeted context (design §9.4 step 7). The counters exist so
    the tool, the metrics (task 7.9) and the "no supporting guidance" behaviour (requirement 17.10)
    can all read the same numbers: ``candidate_count`` is how many distinct chunks the two
    retrievers surfaced before fusion, ``reranked`` records whether Bedrock rerank actually ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passages: tuple[Passage, ...] = ()
    candidate_count: int = 0
    reranked: bool = False

    @property
    def is_empty(self) -> bool:
        """True when nothing cleared the floor — the "no guidance found" signal (req 17.10)."""
        return len(self.passages) == 0


__all__ = [
    "Candidate",
    "DocumentMeta",
    "KnowledgeDomain",
    "KnowledgeFilters",
    "KnowledgeSearchResult",
    "Passage",
    "SectionRef",
]
