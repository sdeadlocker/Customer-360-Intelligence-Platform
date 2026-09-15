"""Knowledge schema, extension and ingestion tests (tasks 7.2, 7.4).

Covers what the Phase 7 gate needs proven before retrieval is built on top: the ``sqlite-vec``
extension loads, the schema builds (including the ``vec0`` table that needs the extension),
ingestion produces documents/chunks/embeddings consistently, re-ingestion is byte-stable, and the
content-hash embedding cache actually saves work.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from c360.data.engine import AccessMode
from c360.knowledge.embeddings import (
    EmbeddingCache,
    MockEmbeddingProvider,
    embedding_cache_key,
)
from c360.knowledge.engine import create_knowledge_engine
from c360.knowledge.extension import load_vec, vec_available, vec_version
from c360.knowledge.ingest import ingest_knowledge
from c360.knowledge.readiness import knowledge_index_state
from c360.knowledge.schema import MANAGED_TABLES, create_statements

_DIMENSIONS = 256  # smaller than the 1024 default keeps the test DB tiny and fast


@pytest.fixture
def knowledge_db(tmp_path: Path) -> Path:
    """A freshly ingested knowledge database from the real corpus, at reduced dimensionality."""
    path = tmp_path / "knowledge.db"
    ingest_knowledge(database=path, dimensions=_DIMENSIONS)
    return path


# ---------------------------------------------------------------- extension


class TestExtension:
    def test_vec_is_available(self) -> None:
        assert vec_available() is True

    def test_vec_version_is_reported(self) -> None:
        version = vec_version()
        assert version is not None
        assert version.startswith("v")

    def test_load_vec_enables_a_vec0_table(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            load_vec(connection)
            connection.execute("CREATE VIRTUAL TABLE t USING vec0(id TEXT PRIMARY KEY, e float[4])")
            # Loading must leave extension loading turned back off.
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("SELECT load_extension('nonexistent')")
        finally:
            connection.close()


# ---------------------------------------------------------------- schema


class TestSchema:
    def test_create_statements_build_every_table(self, tmp_path: Path) -> None:
        path = tmp_path / "schema.db"
        engine = create_knowledge_engine(path, mode=AccessMode.READ_WRITE_CREATE, pool_size=1)
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            for statement in create_statements(_DIMENSIONS):
                cursor.execute(statement)
            raw.commit()
            tables = {
                row[0]
                for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
        finally:
            raw.close()
            engine.dispose()
        for managed in MANAGED_TABLES:
            assert managed in tables, managed

    def test_domain_check_constraint_rejects_unknown_domain(self, tmp_path: Path) -> None:
        path = tmp_path / "schema.db"
        engine = create_knowledge_engine(path, mode=AccessMode.READ_WRITE_CREATE, pool_size=1)
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            for statement in create_statements(_DIMENSIONS):
                cursor.execute(statement)
            with pytest.raises(sqlite3.IntegrityError):
                cursor.execute(
                    "INSERT INTO kb_document (doc_id, title, domain, version, effective_from, "
                    "access_level, content_hash, ingested_at) "
                    "VALUES ('d','t','not_a_domain','v1','2025-01-01','PUBLIC','h','2025-01-01')"
                )
        finally:
            raw.close()
            engine.dispose()


# ---------------------------------------------------------------- ingestion


class TestIngestion:
    def test_ingests_the_full_corpus(self, knowledge_db: Path) -> None:
        state = knowledge_index_state(knowledge_db)
        # 60 document versions in the manifest.
        assert state.documents == 60
        assert state.chunks > 0
        # Every chunk has an embedding — the readiness "ready" invariant.
        assert state.chunks == state.embeddings
        assert state.ready is True

    def test_reingestion_is_byte_stable_for_chunk_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "knowledge.db"
        ingest_knowledge(database=path, dimensions=_DIMENSIONS)
        first = _chunk_ids(path)
        ingest_knowledge(database=path, dimensions=_DIMENSIONS)
        second = _chunk_ids(path)
        assert first == second

    def test_every_domain_is_present(self, knowledge_db: Path) -> None:
        connection = sqlite3.connect(str(knowledge_db))
        try:
            domains = {
                row[0]
                for row in connection.execute("SELECT DISTINCT domain FROM kb_document").fetchall()
            }
        finally:
            connection.close()
        assert domains == {
            "product_catalog",
            "policy",
            "procedure",
            "offer_terms",
            "playbook",
            "compliance",
        }

    def test_superseded_versions_are_retained(self, knowledge_db: Path) -> None:
        """A superseded doc keeps both versions, with the older one's effective_to set (17.3)."""
        connection = sqlite3.connect(str(knowledge_db))
        try:
            rows = connection.execute(
                "SELECT version, effective_to FROM kb_document WHERE doc_id = 'pol-overdraft' "
                "ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        versions = {version for version, _ in rows}
        assert versions == {"v1", "v2"}
        effective_to = dict(rows)
        assert effective_to["v1"] is not None  # superseded
        assert effective_to["v2"] is None  # current

    def test_vector_search_returns_results(self, knowledge_db: Path) -> None:
        import sqlite_vec  # noqa: PLC0415

        query = MockEmbeddingProvider().embed(["eligibility"], dimensions=_DIMENSIONS)[0]
        connection = sqlite3.connect(str(knowledge_db))
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            rows = connection.execute(
                "SELECT chunk_id, distance FROM kb_chunk_vec "
                "WHERE embedding MATCH ? AND k = 5 ORDER BY distance",
                (sqlite_vec.serialize_float32(query),),
            ).fetchall()
        finally:
            connection.close()
        assert len(rows) == 5


def _chunk_ids(path: Path) -> list[str]:
    connection = sqlite3.connect(str(path))
    try:
        return [
            row[0] for row in connection.execute("SELECT chunk_id FROM kb_chunk ORDER BY chunk_id")
        ]
    finally:
        connection.close()


# ---------------------------------------------------------------- embedding cache


class TestEmbeddingCache:
    def test_repeated_text_is_served_from_cache(self) -> None:
        cache = EmbeddingCache(MockEmbeddingProvider())
        cache.embed_cached(["alpha", "beta"], dimensions=_DIMENSIONS)
        cache.embed_cached(["alpha", "gamma"], dimensions=_DIMENSIONS)
        # alpha computed once, beta once, gamma once -> 3 misses; alpha's second time -> 1 hit.
        assert cache.misses == 3
        assert cache.hits == 1

    def test_cache_is_deterministic_and_normalised(self) -> None:
        provider = MockEmbeddingProvider()
        first = provider.embed(["same text"], dimensions=_DIMENSIONS)[0]
        second = provider.embed(["same text"], dimensions=_DIMENSIONS)[0]
        assert first == second
        norm = sum(component * component for component in first) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_key_depends_on_model_and_dimensions(self) -> None:
        base = embedding_cache_key("text", dimensions=1024, model_id="m1")
        assert base != embedding_cache_key("text", dimensions=512, model_id="m1")
        assert base != embedding_cache_key("text", dimensions=1024, model_id="m2")
        assert base == embedding_cache_key("text", dimensions=1024, model_id="m1")
