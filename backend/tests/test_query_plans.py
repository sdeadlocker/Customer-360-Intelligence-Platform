"""Phase 16.1: SQLite read-performance verification.

Task 16.1 asks for ``EXPLAIN QUERY PLAN`` on every hot query, ``ANALYZE`` after seed, a
right-sized read pool, and proof that WAL readers do not block. This module is the executable
form of all four:

* **Query plans.** For each hot read (transaction history, holdings, identifier lookups, graph
  traversal, knowledge FTS) the plan must show an indexed search rather than a full table scan.
  A regression that drops an index, or a query rewrite that defeats one, turns a ``SEARCH ...
  USING INDEX`` into ``SCAN`` — which these tests catch before it reaches a latency budget.
* **ANALYZE after seed.** ``recompute`` runs ``ANALYZE`` (design §4.5), and the seeding pipeline
  always runs recompute before the database is served, so ``sqlite_stat1`` must be populated on a
  recomputed database. The planner needs those statistics to choose the composite indexes.
* **Read pool.** The pool is bounded with no overflow, so a burst cannot silently exceed the
  per-instance connection budget (design §12.3).
* **WAL readers do not block.** A reader holding a transaction open must not stop a writer from
  committing, and vice versa — the whole reason the file is in WAL mode.

The dataset is the real seeded generator output at the default seed, recomputed once for the
module, because a hand-written fixture would not populate the statistics the planner reads or
exercise the composite indexes on realistic row counts.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.pool import QueuePool

from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.recompute import recompute
from c360.generator.pipeline import seed
from c360.knowledge.engine import create_knowledge_engine
from c360.knowledge.extension import vec_available
from c360.knowledge.ingest import ingest_knowledge

SEED = 42
COUNT = 100


# ==================================================================== fixtures
@pytest.fixture(scope="module")
def recomputed_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A seeded, recomputed database built once for the module.

    Recompute is what runs ``ANALYZE`` and builds the derived tables, the search index and the
    graph projection, so the query plans below see the same statistics and indexes the running
    API sees.
    """
    path = tmp_path_factory.mktemp("query_plans") / "customer.db"
    seed(database=path, count=COUNT, seed=SEED)
    recompute(path)
    return path


@pytest.fixture(scope="module")
def reader(recomputed_db: Path) -> Iterator[Engine]:
    engine = create_sqlite_engine(recomputed_db, mode=AccessMode.READ_ONLY, pool_size=2)
    try:
        yield engine
    finally:
        engine.dispose()


def _plan(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> str:
    """Return the flattened ``EXPLAIN QUERY PLAN`` detail text for ``sql``.

    Every row's final column is the human-readable plan step (``SEARCH ... USING INDEX ...`` or
    ``SCAN ...``); joining them gives one string the assertions can search.
    """
    with engine.connect() as connection:
        rows = connection.execute(text(f"EXPLAIN QUERY PLAN {sql}"), params or {}).fetchall()
    return " ".join(str(row[-1]) for row in rows)


def _first_customer_id(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(text("SELECT customer_id FROM customer LIMIT 1")).scalar_one()
        )


def _first_account_id(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT account_id FROM account LIMIT 1")).scalar_one())


# ==================================================================== ANALYZE after seed
def test_analyze_populated_statistics(reader: Engine) -> None:
    """A recomputed database carries planner statistics, without which composite indexes lose."""
    with reader.connect() as connection:
        exists = connection.execute(
            text("SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'")
        ).scalar_one()
        assert exists == 1
        rows = connection.execute(text("SELECT COUNT(*) FROM sqlite_stat1")).scalar_one()
        assert rows > 0


# ==================================================================== hot query plans
def test_transaction_history_uses_the_customer_date_index(reader: Engine) -> None:
    """The most frequent read — a customer's recent transactions, newest first."""
    plan = _plan(
        reader,
        "SELECT * FROM txn WHERE customer_id = :c ORDER BY transaction_date DESC LIMIT 50",
        {"c": _first_customer_id(reader)},
    )
    assert "SCAN txn" not in plan
    assert "ix_txn_cust_date" in plan


def test_transaction_by_category_uses_the_category_index(reader: Engine) -> None:
    plan = _plan(
        reader,
        "SELECT * FROM txn WHERE customer_id = :c AND transaction_category = 'GROCERIES'",
        {"c": _first_customer_id(reader)},
    )
    assert "SCAN txn" not in plan
    assert "ix_txn_cust_cat" in plan


def test_transactions_by_account_uses_the_account_index(reader: Engine) -> None:
    plan = _plan(
        reader,
        "SELECT * FROM txn WHERE account_id = :a ORDER BY transaction_date DESC",
        {"a": _first_account_id(reader)},
    )
    assert "SCAN txn" not in plan
    assert "ix_txn_acct_date" in plan


def test_holdings_use_the_customer_type_index(reader: Engine) -> None:
    plan = _plan(
        reader,
        "SELECT * FROM account WHERE customer_id = :c",
        {"c": _first_customer_id(reader)},
    )
    assert "SCAN account" not in plan
    assert "ix_account_cust_type" in plan


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        ("SELECT * FROM account WHERE account_number = 'X'", "ix_account_number"),
        ("SELECT * FROM loan WHERE loan_number = 'X'", "ix_loan_number"),
        ("SELECT * FROM credit_card WHERE card_last4 = '1234'", "ix_card_last4"),
    ],
)
def test_identifier_lookups_use_their_indexes(reader: Engine, sql: str, index: str) -> None:
    """Every direct-identifier search key (requirement 3.1) must be an indexed lookup."""
    plan = _plan(reader, sql)
    assert index in plan


def test_customer_by_household_uses_the_household_index(reader: Engine) -> None:
    """Household rollups fan out from a household id; the reverse lookup needs its own index."""
    plan = _plan(
        reader,
        "SELECT customer_id FROM customer WHERE household_id = :h",
        {"h": "H-00001"},
    )
    assert "SCAN customer" not in plan
    assert "ix_customer_household" in plan


def test_graph_adjacency_traversal_uses_the_source_index(reader: Engine) -> None:
    """The §5.2 optimization: a single-predicate ``src_id`` hop is an indexed search, not a scan."""
    plan = _plan(
        reader,
        "SELECT dst_id FROM graph_adjacency WHERE src_id = :s AND edge_type = 'OWNS'",
        {"s": "CUSTOMER:C-00001"},
    )
    assert "SCAN graph_adjacency" not in plan
    assert "ix_adj_src" in plan


def test_customer_search_index_is_used(reader: Engine) -> None:
    """A name search hits the FTS5 virtual table rather than scanning the customer table."""
    plan = _plan(
        reader,
        "SELECT c.customer_id FROM customer_search cs "
        "JOIN customer c ON c.customer_id = cs.customer_id "
        "WHERE customer_search MATCH :m LIMIT 10",
        {"m": '"a"*'},
    )
    # The FTS MATCH is served by the virtual-table index (``VIRTUAL TABLE INDEX``), not by
    # scanning rows; the plan labels the ``customer_search`` source by its alias ``cs``.
    assert "VIRTUAL TABLE INDEX" in plan
    # The join back to customer resolves by primary key, never a scan of the whole table.
    assert "SCAN c " not in plan
    assert not plan.endswith("SCAN c")


# ==================================================================== retrieval plans
_KB_DIMENSIONS = 256


@pytest.fixture(scope="module")
def knowledge_reader(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """A freshly ingested knowledge database opened read-only for plan inspection.

    Ingestion runs ``ANALYZE`` (task 7.9), so the FTS and metadata-filter plans see real
    statistics, the same way the customer database does after recompute.
    """
    if not vec_available():
        pytest.skip("sqlite-vec extension unavailable in this environment")
    path = tmp_path_factory.mktemp("kb_plans") / "knowledge.db"
    ingest_knowledge(database=path, dimensions=_KB_DIMENSIONS)
    engine = create_knowledge_engine(path, mode=AccessMode.READ_ONLY, pool_size=2)
    try:
        yield engine
    finally:
        engine.dispose()


def test_knowledge_lexical_retrieval_uses_the_fts_index(knowledge_reader: Engine) -> None:
    """The BM25 lexical stage must be served by the FTS5 virtual-table index, not a scan."""
    plan = _plan(
        knowledge_reader,
        "SELECT c.chunk_id FROM kb_chunk_fts f "
        "JOIN kb_chunk c ON c.chunk_id = f.chunk_id "
        "JOIN kb_document d ON d.doc_id = c.doc_id AND d.version = c.version "
        "WHERE kb_chunk_fts MATCH :m LIMIT 20",
        {"m": "eligibility"},
    )
    assert "VIRTUAL TABLE INDEX" in plan
    # The joins back to chunk and document rows resolve by key, never a full scan.
    assert "SCAN c" not in plan
    assert "SCAN d" not in plan


def test_knowledge_metadata_prefilter_is_index_friendly(knowledge_reader: Engine) -> None:
    """The access-level / effective-date pre-filter joins by document key, not by scanning."""
    plan = _plan(
        knowledge_reader,
        "SELECT c.chunk_id FROM kb_chunk c "
        "JOIN kb_document d ON d.doc_id = c.doc_id AND d.version = c.version "
        "WHERE d.access_level = 'PUBLIC' LIMIT 20",
    )
    # The document lookup for each chunk resolves by primary key rather than a nested scan of the
    # whole document table.
    assert "SEARCH d" in plan


# ==================================================================== read pool shape
def test_read_pool_is_bounded_with_no_overflow(reader: Engine) -> None:
    """A burst cannot silently exceed the per-instance connection budget (design §12.3)."""
    assert isinstance(reader.pool, QueuePool)
    assert reader.pool._max_overflow == 0


# ==================================================================== WAL readers do not block
def test_wal_reader_does_not_block_a_writer(recomputed_db: Path, tmp_path: Path) -> None:
    """A reader holding a transaction open must not stop a writer from committing.

    WAL's central promise is reader/writer concurrency: readers see a consistent snapshot from the
    WAL while a writer appends. Opening a read transaction (a real one, by issuing a SELECT) and
    then committing a write on a separate connection proves the write is not blocked by the open
    reader — under the old rollback-journal mode this would deadlock until ``busy_timeout``.
    """
    path = tmp_path / "wal.db"
    shutil.copy(recomputed_db, path)

    reader = create_sqlite_engine(path, mode=AccessMode.READ_ONLY, pool_size=1)
    writer = create_sqlite_engine(path, mode=AccessMode.READ_WRITE, pool_size=1)
    try:
        with reader.connect() as read_conn:
            # Hold a read snapshot open across the write.
            read_conn.execute(text("BEGIN"))
            read_conn.execute(text("SELECT COUNT(*) FROM customer"))
            with writer.begin() as write_conn:
                write_conn.execute(text("UPDATE derived_financial SET computed_at = computed_at"))
            # The writer committed while the reader's transaction was still open — no deadlock.
            read_conn.execute(text("ROLLBACK"))
    finally:
        reader.dispose()
        writer.dispose()
