"""``KnowledgeService`` — the application service over knowledge retrieval (task 7.8, design §9.6).

This is the one place the REST endpoints and the ``knowledge_search`` tool both call, so entitlement
and the query date are derived from the :class:`Principal` identically on both paths. The service
turns a principal into a :class:`KnowledgeFilters` — the principal's ``knowledge_levels`` become the
access-level pre-filter (requirement 17.8) — and delegates ranking to the retriever.

It holds no knowledge of Bedrock, SQL or fusion: those are the retriever's and repository's concern.
What it owns is the mapping from "who is asking" to "what they may retrieve", which is exactly the
control that must not differ between the API and the tool.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from c360.knowledge.models import (
    DocumentMeta,
    KnowledgeDomain,
    KnowledgeFilters,
    KnowledgeSearchResult,
    SectionRef,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from c360.knowledge.repository import KnowledgeRepository
    from c360.knowledge.retrieval import KnowledgeRetriever
    from c360.security.model import KnowledgeLevel, Principal


class KnowledgeService:
    """Entitlement-aware knowledge retrieval and document lookup (design §9.6)."""

    __slots__ = ("_repository", "_retriever")

    def __init__(self, retriever: KnowledgeRetriever, repository: KnowledgeRepository) -> None:
        self._retriever = retriever
        self._repository = repository

    def search(
        self,
        principal: Principal,
        query: str,
        *,
        domains: Iterable[KnowledgeDomain] = (),
        product_code: str | None = None,
        as_of: str | None = None,
        rerank: bool = False,
    ) -> KnowledgeSearchResult:
        """Retrieve passages for ``query``, filtered to what ``principal`` may see (17.8).

        The principal's knowledge levels become the access-level pre-filter, so a role never
        retrieves — or learns of — a document above its entitlement. ``as_of`` defaults to today, so
        a superseded document version is excluded by the effective-date window (17.3). ``rerank`` is
        passed through for the Q&A path (design scopes reranking to Q&A only).
        """
        filters = self._filters(principal, domains=domains, product_code=product_code, as_of=as_of)
        return self._retriever.retrieve(query, filters, rerank=rerank)

    def get_document(
        self, principal: Principal, doc_id: str, version: str | None = None
    ) -> DocumentMeta | None:
        """Document metadata, or ``None`` if it does not exist or the role cannot see it (17.8).

        A document above the role's entitlement is reported as absent, not forbidden, so the
        endpoint cannot become an existence oracle for restricted documents — the same 404-not-403
        discipline the customer endpoints apply to non-entitled customers.
        """
        meta = self._repository.get_document(doc_id, version)
        if meta is None:
            return None
        if meta.access_level not in {str(level) for level in principal.knowledge_levels}:
            return None
        return meta

    def get_passage(self, principal: Principal, chunk_id: str) -> tuple[str, SectionRef] | None:
        """Resolve a cited chunk to its text and citation, if the role may see its document (17.6).

        Used to open a knowledge citation the UI clicked. Entitlement is re-checked against the
        owning document so a stale or guessed chunk id cannot surface restricted content.
        """
        resolved = self._repository.get_chunk(chunk_id)
        if resolved is None:
            return None
        _text, citation = resolved
        if self.get_document(principal, citation.doc_id, citation.version) is None:
            return None
        return resolved

    def _filters(
        self,
        principal: Principal,
        *,
        domains: Iterable[KnowledgeDomain],
        product_code: str | None,
        as_of: str | None,
    ) -> KnowledgeFilters:
        levels: frozenset[KnowledgeLevel] = principal.knowledge_levels
        return KnowledgeFilters(
            knowledge_levels=frozenset(str(level) for level in levels),
            as_of=as_of or datetime.now(tz=UTC).date().isoformat(),
            domains=frozenset(domains),
            product_code=product_code,
        )


__all__ = ["KnowledgeService"]
