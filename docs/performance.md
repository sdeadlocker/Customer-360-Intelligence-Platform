# Performance and resilience

This document records the Phase 16 hardening work: SQLite read tuning, the caching tiers, the
graceful-degradation behaviour under dependency failure, the load-test method and results, and the
horizontal-scaling model. It is meant to be read alongside `design.md` §12 (deployment) and §13
(performance and resilience requirements).

## Latency budgets (design §13)

| Path | p95 budget |
|---|---|
| Deterministic REST endpoints (profile, holdings, expense, offers, journey, risk) | 3 s |
| AI insight / recommendation cards and natural-language Q&A | 5 s |
| Graph traversal (3-hop neighbourhood on the seeded dataset) | 2 s |
| Customer search typeahead and knowledge retrieval (no rerank) | 500 ms |

These are the budgets the load test (§16.4) and the per-endpoint performance tests (Phase 5.9)
assert against.

## 16.1 SQLite read tuning

### PRAGMAs and pool

Connection setup lives in `backend/src/c360/data/engine.py`. Every physical connection gets, from a
SQLAlchemy `connect` listener so pooled reconnections are covered too:

- `foreign_keys = ON`, `busy_timeout = 5000`, `cache_size = -64000` (64 MiB page cache),
  `temp_store = MEMORY`, `mmap_size = 268435456` (256 MiB memory-mapped window).
- `journal_mode = WAL` and `synchronous = NORMAL` are applied only by writers (migrations, the
  seeder, recompute); read-only connections inherit WAL from the file. Issuing `journal_mode = WAL`
  on a `mode=ro` connection raises on a non-WAL file, so it is deliberately excluded from the
  read-only pragma set.

The read pool is a bounded `QueuePool` with `max_overflow=0`: a burst cannot silently exceed the
per-instance connection and page-cache budget. Size is `SQLITE_READ_POOL_SIZE` (default 8, range
1–64); design §12.3 targets roughly twice the CPU count, one connection per worker thread. The
`C360Aggregator` fan-out thread pool is sized from the same setting so the concurrency of the
dashboard fan-out never exceeds the connections available to serve it.

### ANALYZE

`ANALYZE` runs in three places so the planner always has statistics for the composite indexes:

- the seed load path (`generator/loader.py`) after the WAL checkpoint,
- `recompute` (`data/recompute.py`) after rebuilding the derived, search and graph tables,
- knowledge ingestion (`knowledge/ingest.py`) after chunk and embedding load.

### Query plans

`backend/tests/test_query_plans.py` runs `EXPLAIN QUERY PLAN` over every hot read and asserts an
indexed search rather than a full table scan:

| Hot query | Index asserted |
|---|---|
| Transaction history (newest first) | `ix_txn_cust_date` |
| Transactions by category | `ix_txn_cust_cat` |
| Transactions by account | `ix_txn_acct_date` |
| Holdings for a customer | `ix_account_cust_type` |
| Account / loan / card identifier lookup | `ix_account_number` / `ix_loan_number` / `ix_card_last4` |
| Customer-by-household | `ix_customer_household` |
| Graph adjacency hop | `ix_adj_src` |
| Customer name search (FTS5) | virtual-table index |
| Knowledge lexical retrieval (FTS5) | virtual-table index |
| Knowledge metadata pre-filter | `kb_document` key / `ix_kb_document_filter` |

A regression that drops an index or rewrites a query so it can no longer use one turns
`SEARCH ... USING INDEX` into `SCAN`, which these tests fail on.

### WAL readers do not block

`test_query_plans.py::test_wal_reader_does_not_block_a_writer` holds a read transaction open (a real
snapshot, opened by issuing a SELECT) and commits a write on a separate connection. WAL lets the
write proceed without waiting on the reader; under the old rollback-journal mode this would block
until `busy_timeout`.

## 16.2 Caching tiers

Four tiers, all sharing one Redis-ready contract (`core/cache.py` `Cache` protocol: `get` / `put` /
`invalidate` / `invalidate_all`). A Redis backend implements the same protocol, so swapping it in is
a construction-time choice, not a call-site change.

1. **HTTP reference-data caching** — `api/caching.py`. The knowledge document endpoint returns a
   strong `ETag` (hashed over the stable `data` payload, not the per-request `meta`) and
   `Cache-Control: private, max-age=60, must-revalidate`. A repeat read with a matching
   `If-None-Match` gets a bodyless `304`. Only institutional, role-independent data is cached this
   way; entitlement-scoped, per-role customer data is deliberately excluded.
2. **Read-model TTL (60 s)** — `core/cache.py` `TtlCache`, wired onto app state as
   `read_model_cache` and sized by `READ_MODEL_CACHE_TTL_S` (default 60, zero disables). Cleared by
   the recompute path.
3. **Agent output cache (15 min)** — `agents/cache.py`, keyed on everything that can change a
   narrative (facts, knowledge, formula / prompt / model versions), invalidated on recompute. It
   satisfies the shared contract but keeps its own class for the `cache_hit` labelling flag.
4. **Content-hashed embedding cache** — `knowledge/embeddings.py`, so re-ingesting an unchanged
   corpus costs nothing.

The recompute endpoint (`POST /admin/recompute`) invalidates both the agent cache and the
read-model cache in one pass, so derived-value changes never leave a stale narrative or read model.

## 16.3 Graceful degradation

Three circuit breakers (`agents/breaker.py`), each with a fallback (design §13.7):

- **generation** → deterministic template renderer; cards show a degraded badge.
- **embedding** → lexical-only retrieval; the semantic stage is skipped and RRF runs over the BM25
  list alone.
- **rerank** → fusion order.

The knowledge database being absent or unreadable degrades to "no supporting guidance found" rather
than failing the request. `test_degradation.py` (§16.3) asserts each dependency can fail
independently while the dashboard still renders and the degradation is visible to the client.

## 16.4 Load test

_Method and results are recorded here after a run; see `backend/loadtest/locustfile.py`._

## 16.5 Horizontal scaling

Each API instance opens its own read-only replica of the customer and knowledge databases and its
own append-only `audit.db`; audit rollup is a separate offline concern. There is no shared mutable
state on the request path and no sticky-session dependency: auth is stateless RS256 JWT and Q&A
conversation memory is keyed by `(session_id, customer_id)` in a checkpoint DB. Two instances behind
a round-robin load balancer therefore serve any request interchangeably.
