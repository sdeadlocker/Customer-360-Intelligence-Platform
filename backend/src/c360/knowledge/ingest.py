"""Knowledge ingestion — build ``knowledge.db`` from the source corpus (tasks 7.2—7.5).

This is the knowledge counterpart of the customer seeder: it reads the ``knowledge/`` corpus, chunks
each document version (:mod:`c360.knowledge.chunking`), embeds the chunks
(:mod:`c360.knowledge.embeddings`), and writes ``kb_document``, ``kb_chunk``, ``kb_chunk_fts`` and
``kb_chunk_vec`` in a single transaction over a fresh database. Rebuild-wholesale rather than
in-place upgrade, for the reason the schema module gives: the embedding cache makes re-ingesting
cheap, so there is no upgrade path worth preserving.

The write pattern mirrors :mod:`c360.data.recompute`: open read-write-create, do everything in one
transaction, ``PRAGMA foreign_key_check`` before commit, checkpoint the WAL and ``ANALYZE`` after.
The ``kb_chunk_vec`` table is a ``vec0`` virtual table, so the connection must have ``sqlite-vec``
loaded — which is why ingestion goes through :func:`c360.knowledge.engine.create_knowledge_engine`
rather than the plain customer engine.

Determinism (task 7.3)
---------------------
Documents are ingested in manifest order and chunk ids are position-derived, so two ingestions of
the same corpus at the same embedding dimensionality produce byte-identical ``kb_document`` and
``kb_chunk`` rows. The mock embedding provider is deterministic too, so under ``LLM_PROVIDER=mock``
the whole database is reproducible — which is what the Phase 11 evaluation fixtures depend on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlite_vec

from c360.core.logging import get_logger
from c360.data.engine import AccessMode
from c360.knowledge.chunking import Chunk, chunk_document
from c360.knowledge.corpus import CorpusDocument, content_hash, load_corpus
from c360.knowledge.embeddings import EmbeddingCache, EmbeddingProvider, MockEmbeddingProvider
from c360.knowledge.engine import create_knowledge_engine
from c360.knowledge.schema import MANAGED_TABLES, create_statements

_logger = get_logger(__name__)


class IngestionError(RuntimeError):
    """Raised when ingestion fails a referential-integrity check after writing."""


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """Per-run counts and timings, returned for the CLI and asserted on by tests."""

    database: Path
    documents: int
    chunks: int
    embed_hits: int
    embed_misses: int
    dimensions: int
    embed_model_id: str
    elapsed_seconds: float

    def summary_lines(self) -> list[str]:
        """Human-readable summary, for the CLI."""
        return [
            f"database      {self.database}",
            f"elapsed       {self.elapsed_seconds:.2f}s",
            f"embed model   {self.embed_model_id} ({self.dimensions} dims)",
            "",
            "ingested:",
            f"  documents               {self.documents:>9,}",
            f"  chunks                  {self.chunks:>9,}",
            f"  embeddings computed     {self.embed_misses:>9,}",
            f"  embeddings from cache   {self.embed_hits:>9,}",
        ]


def ingest_knowledge(
    *,
    database: Path,
    corpus_dir: Path | None = None,
    provider: EmbeddingProvider | None = None,
    dimensions: int = 1024,
    target_tokens: int = 500,
    overlap_ratio: float = 0.15,
) -> IngestionReport:
    """Build ``knowledge.db`` at ``database`` from the corpus.

    Args:
        database: The target ``knowledge.db`` file. Created (with parents) if absent; its schema is
            dropped and rebuilt if present, so ingestion is idempotent.
        corpus_dir: The ``knowledge/`` directory. Defaults to the repository corpus.
        provider: The embedding provider. Defaults to the deterministic mock, so ingestion runs
            offline with no AWS; a caller wanting real Titan embeddings passes a Bedrock provider.
        dimensions: Embedding dimensionality; must match the ``kb_chunk_vec`` column width, so it is
            used both to build the schema and to embed.
        target_tokens: Soft chunk token target (design §9.5).
        overlap_ratio: Chunk overlap fraction (~0.15).

    Raises:
        CorpusError: the corpus or manifest is malformed.
        IngestionError: a foreign-key violation survived the write.
        VecExtensionError: sqlite-vec cannot be loaded in this environment.
    """
    documents = load_corpus(corpus_dir) if corpus_dir else load_corpus()
    cache = EmbeddingCache(provider if provider is not None else MockEmbeddingProvider())
    started = time.perf_counter()
    ingested_at = datetime.now(UTC).isoformat()

    database.parent.mkdir(parents=True, exist_ok=True)
    engine = create_knowledge_engine(database, mode=AccessMode.READ_WRITE_CREATE, pool_size=1)
    connection = engine.raw_connection()
    total_chunks = 0
    try:
        cursor = connection.cursor()
        _reset_schema(cursor, dimensions)

        for document in documents:
            chunks = chunk_document(
                doc_id=document.doc_id,
                version=document.version,
                body=document.body,
                target_tokens=target_tokens,
                overlap_ratio=overlap_ratio,
            )
            _write_document(cursor, document, ingested_at)
            _write_chunks(cursor, document, chunks, cache, dimensions)
            total_chunks += len(chunks)

        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            connection.rollback()
            raise IngestionError(f"{len(violations)} foreign-key violation(s) after ingestion")

        connection.commit()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cursor.execute("ANALYZE")
        cursor.close()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        engine.dispose()

    elapsed = time.perf_counter() - started
    report = IngestionReport(
        database=database,
        documents=len(documents),
        chunks=total_chunks,
        embed_hits=cache.hits,
        embed_misses=cache.misses,
        dimensions=dimensions,
        embed_model_id=cache.model_id,
        elapsed_seconds=elapsed,
    )
    _logger.info(
        "knowledge ingestion complete",
        extra={
            "database": str(database),
            "documents": report.documents,
            "chunks": report.chunks,
            "elapsed_seconds": round(elapsed, 3),
        },
    )
    return report


def _reset_schema(cursor: Any, dimensions: int) -> None:
    """Drop any existing managed tables and recreate the schema for ``dimensions``."""
    for table in MANAGED_TABLES:
        cursor.execute(f"DROP TABLE IF EXISTS {table}")
    for statement in create_statements(dimensions):
        cursor.execute(statement)


def _write_document(cursor: Any, document: CorpusDocument, ingested_at: str) -> None:
    cursor.execute(
        "INSERT INTO kb_document ("
        "doc_id, title, domain, version, effective_from, effective_to, jurisdiction, "
        "access_level, product_code, business_group, source_uri, content_hash, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document.doc_id,
            document.title,
            document.domain.value,
            document.version,
            document.effective_from,
            document.effective_to,
            document.jurisdiction,
            document.access_level,
            document.product_code,
            document.business_group,
            document.source_path,
            content_hash(document.body),
            ingested_at,
        ),
    )


def _write_chunks(
    cursor: Any,
    document: CorpusDocument,
    chunks: list[Chunk],
    cache: EmbeddingCache,
    dimensions: int,
) -> None:
    if not chunks:
        return
    vectors = cache.embed_cached([chunk.text for chunk in chunks], dimensions=dimensions)
    for chunk, vector in zip(chunks, vectors, strict=True):
        cursor.execute(
            "INSERT INTO kb_chunk ("
            "chunk_id, doc_id, version, section_path, ordinal, text, token_count, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                chunk.doc_id,
                chunk.version,
                chunk.section_path,
                chunk.ordinal,
                chunk.text,
                chunk.token_count,
                content_hash(chunk.text),
            ),
        )
        cursor.execute(
            "INSERT INTO kb_chunk_fts (chunk_id, text, section_path) VALUES (?, ?, ?)",
            (chunk.chunk_id, chunk.text, chunk.section_path),
        )
        cursor.execute(
            "INSERT INTO kb_chunk_vec (chunk_id, embedding) VALUES (?, ?)",
            (chunk.chunk_id, sqlite_vec.serialize_float32(vector)),
        )


__all__ = ["IngestionError", "IngestionReport", "ingest_knowledge"]
