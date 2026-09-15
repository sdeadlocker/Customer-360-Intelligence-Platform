"""Knowledge index state, for readiness reporting (task 7.2, requirement 18.14).

A small, side-effect-free read of ``knowledge.db`` that answers "is the index built and internally
consistent" without loading the retrieval stack. Opened read-only with ``sqlite-vec`` loaded (the
``kb_chunk_vec`` count needs the extension), and every count guarded so a half-built or empty
database reports rather than raises — a readiness probe reports state, it never fails the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from c360.core.logging import get_logger
from c360.data.engine import AccessMode, DatabaseFileMissingError
from c360.knowledge.engine import create_knowledge_engine

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeIndexState:
    """Counts describing the built knowledge index. ``ready`` is the readiness verdict."""

    documents: int
    chunks: int
    embeddings: int

    @property
    def ready(self) -> bool:
        """Ready when there is content and every chunk has an embedding.

        A chunk without a vector is a semantically un-retrievable chunk, so a mismatch between the
        chunk and embedding counts is an inconsistent index — reported as not-ready rather than
        silently degrading the semantic half of retrieval.
        """
        return self.documents > 0 and self.chunks > 0 and self.chunks == self.embeddings


def knowledge_index_state(db_path: Path) -> KnowledgeIndexState:
    """Read the document, chunk and embedding counts from ``knowledge.db``.

    Returns zeros when the database is absent or unreadable, so a caller can treat "not built" and
    "empty" uniformly. Never raises for an operational condition — only genuinely unexpected errors
    propagate, and the readiness registry turns even those into a failing check.
    """
    try:
        engine = create_knowledge_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=1)
    except DatabaseFileMissingError:
        return KnowledgeIndexState(documents=0, chunks=0, embeddings=0)

    try:
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            documents = _count(cursor, "kb_document")
            chunks = _count(cursor, "kb_chunk")
            embeddings = _count(cursor, "kb_chunk_vec")
            cursor.close()
        finally:
            raw.close()
    finally:
        engine.dispose()

    return KnowledgeIndexState(documents=documents, chunks=chunks, embeddings=embeddings)


def _count(cursor: object, table: str) -> int:
    """``SELECT count(*)`` from ``table``, returning 0 if the table is absent."""
    try:
        row = cursor.execute(f"SELECT count(*) FROM {table}").fetchone()  # type: ignore[attr-defined]  # noqa: S608
    except Exception as exc:
        _logger.debug("knowledge count failed", extra={"table": table, "error": type(exc).__name__})
        return 0
    return int(row[0]) if row and row[0] is not None else 0


__all__ = ["KnowledgeIndexState", "knowledge_index_state"]
