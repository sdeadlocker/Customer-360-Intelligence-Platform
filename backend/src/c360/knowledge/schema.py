"""The ``knowledge.db`` schema (task 7.2, design §9.3).

Unlike the customer database, the knowledge database is not migrated with Alembic. It is a
projection of the ``knowledge/`` source corpus, rebuilt wholesale by ingestion (task 7.3—7.5) the
same way the customer database is built by the seeder — so its schema is created once, inside the
ingestion transaction, from the literal DDL here rather than from a migration history. There is no
in-place upgrade path to preserve: a schema change means re-ingesting, which is cheap because the
embedding cache (task 7.4) means unchanged documents cost nothing to re-embed.

The four tables are transcribed verbatim from design §9.3:

* ``kb_document`` — one row per document *version*. ``UNIQUE (doc_id, version)`` because a changed
  document becomes a new version sharing the ``doc_id`` (requirement 17.3), and both rows coexist.
* ``kb_chunk`` — the retrievable units, with the deterministic ``chunk_id`` that keeps evaluation
  fixtures stable across re-ingestion (design §9.3).
* ``kb_chunk_fts`` — the FTS5 lexical half. ``chunk_id`` is ``UNINDEXED`` so a hit joins back to
  ``kb_chunk`` for metadata without being tokenized.
* ``kb_chunk_vec`` — the ``sqlite-vec`` semantic half, ``float[1024]`` by default. This is why the
  connection must have the extension loaded (see :mod:`c360.knowledge.engine`).

The two btree indexes on ``kb_chunk`` back the metadata pre-filter (design §9.4 step 2), which
resolves a chunk's document and its access level / effective window before ranking. The pre-filter
joins ``kb_chunk`` to ``kb_document``, so the join key and the document's filter columns both need
to be reachable without a scan.
"""

from __future__ import annotations

from typing import Final

#: The embedding dimensionality the ``kb_chunk_vec`` table is declared with. Titan Text Embeddings
#: V2 supports 256/512/1024; the schema is built for the configured value so the vector column width
#: matches what ingestion writes. Design §9.3 shows 1024, which is the project default.
DEFAULT_EMBED_DIMENSIONS: Final = 1024

#: Table names this module owns, in the order they are dropped (children first). Used by the
#: ingestion rebuild to clear an existing database before recreating it.
MANAGED_TABLES: Final[tuple[str, ...]] = (
    "kb_chunk_vec",
    "kb_chunk_fts",
    "kb_chunk",
    "kb_document",
)


def create_statements(embed_dimensions: int = DEFAULT_EMBED_DIMENSIONS) -> tuple[str, ...]:
    """The ordered DDL that builds an empty knowledge schema.

    ``embed_dimensions`` widths the ``kb_chunk_vec`` vector column. It is a parameter rather than a
    constant because design §9.3 names 512 as the first lever to pull on latency, and the vector
    column width has to agree with the embedding the provider is configured to produce — a mismatch
    is a ``sqlite-vec`` insert error, not a silent truncation.
    """
    return (
        # ---------------------------------------------------------------- kb_document
        """
        CREATE TABLE kb_document (
          doc_id         TEXT NOT NULL,
          title          TEXT NOT NULL,
          domain         TEXT NOT NULL CHECK (domain IN
                           ('product_catalog','policy','procedure','offer_terms',
                            'playbook','compliance')),
          version        TEXT NOT NULL,
          effective_from TEXT NOT NULL CHECK (effective_from IS date(effective_from)),
          effective_to   TEXT CHECK (effective_to IS NULL OR effective_to IS date(effective_to)),
          jurisdiction   TEXT,
          access_level   TEXT NOT NULL CHECK (access_level IN
                           ('PUBLIC','INTERNAL','RISK_ONLY','COMPLIANCE_ONLY')),
          product_code   TEXT,
          business_group TEXT,
          source_uri     TEXT,
          content_hash   TEXT NOT NULL,
          ingested_at    TEXT NOT NULL,
          PRIMARY KEY (doc_id, version)
        )
        """,
        # A document is looked up by doc_id across versions (the versioning filter picks the row
        # effective as of the query date), so doc_id needs an index of its own beyond the composite
        # PK whose leading column it already is — the PK covers this, but the domain/access filter
        # below benefits from a dedicated covering index for the pre-filter's WHERE.
        "CREATE INDEX ix_kb_document_filter ON kb_document(access_level, domain, effective_from)",
        "CREATE INDEX ix_kb_document_product ON kb_document(product_code)",
        # ---------------------------------------------------------------- kb_chunk
        """
        CREATE TABLE kb_chunk (
          chunk_id     TEXT PRIMARY KEY,
          doc_id       TEXT NOT NULL,
          version      TEXT NOT NULL,
          section_path TEXT NOT NULL,
          ordinal      INTEGER NOT NULL,
          text         TEXT NOT NULL,
          token_count  INTEGER NOT NULL CHECK (token_count >= 0),
          content_hash TEXT NOT NULL,
          FOREIGN KEY (doc_id, version) REFERENCES kb_document(doc_id, version)
        )
        """,
        # The pre-filter joins kb_chunk to kb_document on (doc_id, version); this index makes that
        # join an indexed lookup rather than a scan of every chunk.
        "CREATE INDEX ix_kb_chunk_doc ON kb_chunk(doc_id, version)",
        # ---------------------------------------------------------------- kb_chunk_fts (lexical)
        # FTS5 is compiled in, so this needs no extension. `chunk_id` UNINDEXED carries the join key
        # into the index without tokenizing it; `text` and `section_path` are the searchable
        # columns. The tokenizer matches design §9.3.
        """
        CREATE VIRTUAL TABLE kb_chunk_fts USING fts5(
          chunk_id UNINDEXED,
          text,
          section_path,
          tokenize = "unicode61 remove_diacritics 2"
        )
        """,
        # ---------------------------------------------------------------- kb_chunk_vec (semantic)
        # A vec0 virtual table; requires the sqlite-vec extension on the connection. The vector
        # column width is the configured embedding dimensionality. `distance_metric=cosine` makes
        # the KNN `MATCH` return cosine distance (design §9.4 step 4: "cosine distance over
        # kb_chunk_vec"), so the semantic retriever ranks by angle rather than L2 magnitude — the
        # right choice for text embeddings, whose magnitude carries no meaning.
        f"""
        CREATE VIRTUAL TABLE kb_chunk_vec USING vec0(
          chunk_id TEXT PRIMARY KEY,
          embedding float[{int(embed_dimensions)}] distance_metric=cosine
        )
        """,
    )


__all__ = ["DEFAULT_EMBED_DIMENSIONS", "MANAGED_TABLES", "create_statements"]
