"""``KnowledgeRepository`` — the SQL side of hybrid retrieval (task 7.5, design §9.4, §9.6).

Two retrievers, one metadata pre-filter. The pre-filter is the load-bearing security control: it is
applied **in SQL, before ranking** (design §9.4 step 2), so a document whose access level is above
the caller's entitlement never enters the candidate set and its existence cannot be inferred from a
result count (requirement 17.8). The same clause bounds the effective-date window so a superseded
version is excluded (requirement 17.3) and narrows by domain and product code when asked.

Both retrievers join their virtual table (``kb_chunk_fts`` / ``kb_chunk_vec``) to ``kb_chunk`` and
``kb_document`` and apply that identical clause, so lexical and semantic candidates are drawn from
exactly the same eligible set. Returning them with their per-retriever *rank* (not score) is what
lets the service fuse them with Reciprocal Rank Fusion without calibrating two different scales.

Instrumentation follows the repository convention (:mod:`c360.data.repositories.base`): each read is
a span carrying a stable statement id and the row count, never the SQL or the bound query text — the
query is redacted upstream (task 7.7) but the span still must not carry it, because design §13.4
keeps statements out of telemetry regardless.
"""

from __future__ import annotations

from typing import Any

import sqlite_vec
from sqlalchemy import Engine, Row, text
from sqlalchemy.exc import SQLAlchemyError

from c360.core.logging import get_logger
from c360.core.telemetry import SpanAttr, get_tracer
from c360.knowledge.embeddings import Vector
from c360.knowledge.models import (
    Candidate,
    DocumentMeta,
    KnowledgeDomain,
    KnowledgeFilters,
    SectionRef,
)

_logger = get_logger(__name__)

_TRACER_NAME = "c360.knowledge.repository"
_DB_SYSTEM = "sqlite"

#: FTS5 query-syntax characters, stripped before a user phrase is wrapped as a quoted MATCH term —
#: the same defence the customer search repository applies (see `build_kb_match`).
_FTS_SYNTAX = str.maketrans(dict.fromkeys('"*:^()-+' + "'", " "))


def build_kb_match(raw: str) -> str | None:
    """Turn a free-text query into a safe FTS5 MATCH expression, or ``None`` if it is empty.

    Each token is stripped of FTS operator characters and wrapped as a quoted phrase with a trailing
    ``*`` for prefix matching; tokens are joined by implicit AND. Mirrors
    :func:`c360.data.repositories.customer.build_fts_query` so a stray quote or colon in a query can
    never become a malformed MATCH (a syntax error) or an unintended column filter. Returns ``None``
    for input that is empty once stripped, so the caller answers with no results rather than issuing
    a MATCH FTS5 would reject.
    """
    tokens = [token for token in raw.translate(_FTS_SYNTAX).split() if token]
    if not tokens:
        return None
    return " OR ".join(f'"{token}"*' for token in tokens)


class KnowledgeRepository:
    """Read adapter over ``knowledge.db`` for hybrid retrieval and document lookup (design §9.6).

    Holds the knowledge engine (built with ``sqlite-vec`` loaded) and nothing else, so one instance
    is shared across the read pool exactly like the customer repositories.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---------------------------------------------------------------- pre-filter SQL
    @staticmethod
    def _filter_clause(filters: KnowledgeFilters) -> tuple[str, dict[str, object]]:
        """Build the parameterized metadata pre-filter (design §9.4 step 2).

        Returns a SQL fragment for the ``WHERE`` and its bindings. The access-level ``IN`` list is
        built from parameterized placeholders — never interpolated values — the same way the
        customer repositories build a scoped ``IN``. An empty ``knowledge_levels`` yields a clause
        that matches nothing (``1 = 0``), which is the correct answer for a caller entitled to no
        knowledge level: no candidates, no leakage.
        """
        params: dict[str, object] = {"as_of": filters.as_of}
        clauses: list[str] = []

        levels = sorted(filters.knowledge_levels)
        if not levels:
            return "1 = 0", params
        level_placeholders = ", ".join(f":level_{i}" for i in range(len(levels)))
        for i, level in enumerate(levels):
            params[f"level_{i}"] = level
        clauses.append(f"d.access_level IN ({level_placeholders})")

        # Effective-date window: the version in force as of the query date (requirement 17.3).
        clauses.append("d.effective_from <= :as_of")
        clauses.append("(d.effective_to IS NULL OR :as_of <= d.effective_to)")

        domains = sorted(domain.value for domain in filters.domains)
        if domains:
            domain_placeholders = ", ".join(f":domain_{i}" for i in range(len(domains)))
            for i, domain in enumerate(domains):
                params[f"domain_{i}"] = domain
            clauses.append(f"d.domain IN ({domain_placeholders})")

        if filters.product_code is not None:
            clauses.append("d.product_code = :product_code")
            params["product_code"] = filters.product_code

        return " AND ".join(clauses), params

    # ---------------------------------------------------------------- lexical (FTS5 BM25)
    def lexical(self, query: str, filters: KnowledgeFilters, k: int) -> list[Candidate]:
        """Top-``k`` chunks by FTS5 BM25, restricted to the pre-filtered eligible set (design §9.4).

        ``bm25()`` returns a score where *more negative is more relevant*, so ordering ascending
        gives best-first. The pre-filter is joined in, not applied afterwards, so a chunk from a
        document the caller cannot see is never even ranked.
        """
        match = build_kb_match(query)
        if match is None:
            return []
        clause, params = self._filter_clause(filters)
        params["match"] = match
        params["k"] = k
        sql = f"""
            SELECT c.chunk_id, c.doc_id, c.version, c.section_path, c.text
            FROM kb_chunk_fts f
            JOIN kb_chunk c ON c.chunk_id = f.chunk_id
            JOIN kb_document d ON d.doc_id = c.doc_id AND d.version = c.version
            WHERE kb_chunk_fts MATCH :match AND {clause}
            ORDER BY bm25(kb_chunk_fts)
            LIMIT :k
        """  # noqa: S608 - clause is parameterized placeholders, never interpolated values
        rows = self._run("knowledge.lexical", "kb_chunk_fts", sql, params)
        return [self._candidate(row, rank) for rank, row in enumerate(rows, start=1)]

    # ---------------------------------------------------------------- semantic (vec0 cosine)
    def semantic(self, embedding: Vector, filters: KnowledgeFilters, k: int) -> list[Candidate]:
        """Top-``k`` chunks by ``sqlite-vec`` cosine distance within the eligible set (§9.4).

        The pre-filter cannot be applied inside the ``vec0`` KNN sub-select — a ``vec0`` MATCH must
        stand alone — so the vector search takes a widened ``k`` and the eligibility join filters
        its output, then the outer ``LIMIT`` trims to ``k``. Distance is cosine (the table declares
        ``distance_metric=cosine``), so ascending order is nearest-first.
        """
        clause, params = self._filter_clause(filters)
        params["query_vec"] = sqlite_vec.serialize_float32(embedding)
        params["k"] = k
        # Over-fetch from the vector index so post-filtering to the eligible set still leaves k.
        params["knn_k"] = max(k * 4, k + 20)
        sql = f"""
            SELECT c.chunk_id, c.doc_id, c.version, c.section_path, c.text
            FROM (
                SELECT chunk_id, distance
                FROM kb_chunk_vec
                WHERE embedding MATCH :query_vec AND k = :knn_k
                ORDER BY distance
            ) v
            JOIN kb_chunk c ON c.chunk_id = v.chunk_id
            JOIN kb_document d ON d.doc_id = c.doc_id AND d.version = c.version
            WHERE {clause}
            ORDER BY v.distance
            LIMIT :k
        """  # noqa: S608 - clause is parameterized placeholders, never interpolated values
        rows = self._run("knowledge.semantic", "kb_chunk_vec", sql, params)
        return [self._candidate(row, rank) for rank, row in enumerate(rows, start=1)]

    # ---------------------------------------------------------------- point reads
    def get_chunk(self, chunk_id: str) -> tuple[str, SectionRef] | None:
        """The text and citation for one chunk, or ``None`` if it does not exist.

        Not entitlement-filtered: a chunk id is only ever handed out by a retrieval call that has
        already applied the pre-filter, so a caller cannot fetch a chunk it was not shown. Used to
        resolve a citation the UI clicked (requirement 17.6).
        """
        sql = """
            SELECT c.chunk_id, c.doc_id, c.version, c.section_path,
                   d.title, d.effective_from, d.effective_to
            FROM kb_chunk c
            JOIN kb_document d ON d.doc_id = c.doc_id AND d.version = c.version
            WHERE c.chunk_id = :chunk_id
        """
        text_sql = """
            SELECT text FROM kb_chunk WHERE chunk_id = :chunk_id
        """
        rows = self._run("knowledge.get_chunk", "kb_chunk", sql, {"chunk_id": chunk_id})
        if not rows:
            return None
        row = rows[0]
        text_rows = self._run(
            "knowledge.get_chunk_text", "kb_chunk", text_sql, {"chunk_id": chunk_id}
        )
        chunk_text = str(text_rows[0][0]) if text_rows else ""
        citation = SectionRef(
            chunk_id=str(row[0]),
            doc_id=str(row[1]),
            version=str(row[2]),
            section_path=str(row[3]),
            title=str(row[4]),
            effective_from=str(row[5]),
            effective_to=str(row[6]) if row[6] is not None else None,
        )
        return chunk_text, citation

    def get_document(self, doc_id: str, version: str | None = None) -> DocumentMeta | None:
        """Document metadata, either a named version or the latest by effective date.

        With ``version`` omitted the most recently effective version is returned, so a caller
        asking for ``/knowledge/documents/{doc_id}`` gets the current document, not an arbitrary
        one. Entitlement is enforced at the endpoint (task 7.8), not here.
        """
        if version is not None:
            sql = "SELECT * FROM kb_document WHERE doc_id = :doc_id AND version = :version"
            params: dict[str, object] = {"doc_id": doc_id, "version": version}
        else:
            sql = (
                "SELECT * FROM kb_document WHERE doc_id = :doc_id "
                "ORDER BY effective_from DESC, version DESC LIMIT 1"
            )
            params = {"doc_id": doc_id}
        rows = self._run("knowledge.get_document", "kb_document", sql, params)
        if not rows:
            return None
        row = rows[0]
        mapping = row._mapping
        return DocumentMeta(
            doc_id=str(mapping["doc_id"]),
            title=str(mapping["title"]),
            domain=KnowledgeDomain(str(mapping["domain"])),
            version=str(mapping["version"]),
            effective_from=str(mapping["effective_from"]),
            effective_to=(
                str(mapping["effective_to"]) if mapping["effective_to"] is not None else None
            ),
            jurisdiction=(
                str(mapping["jurisdiction"]) if mapping["jurisdiction"] is not None else None
            ),
            access_level=str(mapping["access_level"]),
            product_code=(
                str(mapping["product_code"]) if mapping["product_code"] is not None else None
            ),
            business_group=(
                str(mapping["business_group"]) if mapping["business_group"] is not None else None
            ),
            source_uri=str(mapping["source_uri"]) if mapping["source_uri"] is not None else None,
        )

    def document_access_level(self, doc_id: str, version: str | None = None) -> str | None:
        """The access level of a document version, for the endpoint's entitlement check (7.8)."""
        meta = self.get_document(doc_id, version)
        return meta.access_level if meta is not None else None

    # ---------------------------------------------------------------- execution
    def _run(
        self, statement_id: str, collection: str, sql: str, params: dict[str, object]
    ) -> list[Row[Any]]:
        """Execute a read inside an instrumented span. Never puts SQL or bindings on the span."""
        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(f"SELECT {collection}") as span:
            span.set_attribute("db.system.name", _DB_SYSTEM)
            span.set_attribute("db.operation.name", "SELECT")
            span.set_attribute("db.collection.name", collection)
            span.set_attribute(SpanAttr.DB_STATEMENT_ID, statement_id)
            try:
                with self._engine.connect() as connection:
                    rows = list(connection.execute(text(sql), params).all())
            except SQLAlchemyError as exc:
                span.set_attribute(SpanAttr.OUTCOME, "error")
                _logger.warning(
                    "knowledge read failed",
                    extra={"statement_id": statement_id, "error_type": type(exc).__name__},
                )
                raise
            span.set_attribute(SpanAttr.DB_ROW_COUNT, len(rows))
            span.set_attribute(SpanAttr.OUTCOME, "ok")
            return rows

    @staticmethod
    def _candidate(row: object, rank: int) -> Candidate:
        return Candidate(
            chunk_id=str(row[0]),  # type: ignore[index]
            doc_id=str(row[1]),  # type: ignore[index]
            version=str(row[2]),  # type: ignore[index]
            section_path=str(row[3]),  # type: ignore[index]
            text=str(row[4]),  # type: ignore[index]
            rank=rank,
        )


__all__ = ["KnowledgeRepository", "build_kb_match"]
