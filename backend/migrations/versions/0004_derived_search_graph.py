"""Derived-value tables, the FTS5 customer search index and the graph projection.

Phase 3 adds three families of *recomputable* structure on top of the source tables. None of it is
authored by the generator: every row here is a projection of rows migrations ``0001``-``0003``
already hold, rebuilt idempotently by the recompute job (:mod:`c360.data.recompute`). The design
principle (§14.6) is that derived state is stored for read speed but never the source of truth — a
test asserts stored values equal freshly-computed ones, and the whole projection is droppable and
rebuildable without touching a source row.

What this migration creates
---------------------------

* **``derived_financial`` / ``derived_credit`` / ``derived_health``** — the ``derived_*`` tables of
  design §4.6 and §7.3. Financial and credit totals mirror the shape of ``financial_profile`` /
  ``credit_profile`` so the recompute job's stored-equals-computed assertion is a column-by-column
  comparison. The health table stores the ``fhs-v1`` heuristic score with its band, provenance,
  formula version and named drivers (§7.3).
* **``customer_search``** — the FTS5 virtual table of design §4.4, with identifier columns
  denormalized so one ``MATCH`` query satisfies every search key in requirement 3.1.
* **``graph_node`` / ``graph_edge`` / ``graph_adjacency``** — the graph projection of design §5.1
  and §5.2. Every logical edge is materialized in both directions in ``graph_adjacency`` so a
  3-hop traversal is a single indexed equality predicate rather than the un-indexable
  ``src_id = :n OR dst_id = :n``.

Why a virtual table lives in a migration
-----------------------------------------

FTS5 is a compiled-in SQLite extension, not a loadable one, so ``CREATE VIRTUAL TABLE ... USING
fts5`` is available on the same connection every other statement runs on. Creating it here — rather
than in the recompute job — keeps the schema in one place and lets the ``0004`` downgrade drop it
cleanly. The recompute job only ever *populates* it (``DELETE`` then ``INSERT``), never redefines
it.

Date columns use ``IS date(x)`` rather than ``= date(x)``; migration ``0001`` explains why.

Revision ID: 0004_derived_search_graph
Revises: 0003_events_offers_assets_relationships
Created: 2026-09-13
"""

from __future__ import annotations

from c360.data.ddl import Statements, drop_tables, execute_all

revision: str = "0004_derived_search_graph"
down_revision: str | None = "0003_events_offers_assets_relationships"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE: Statements = (
    # ================================================================ derived financial
    # Design §4.6. These columns are the recomputable subset of `financial_profile`: the generator
    # writes `financial_profile` from the plan, and the recompute job writes `derived_financial`
    # from the actual account/asset/txn rows. The stored-equals-computed test (task 3.1) compares
    # the two, which is the executable form of "derived values are always recomputable" (§14.6).
    #
    # The CHECK mirrors `financial_profile`'s so an inconsistent projection is rejected at write
    # time rather than surfacing as an agent narrating a net worth that does not add up.
    """
    CREATE TABLE derived_financial (
      customer_id               TEXT PRIMARY KEY REFERENCES customer(customer_id),
      total_deposits_cents      INTEGER NOT NULL DEFAULT 0,
      total_loans_cents         INTEGER NOT NULL DEFAULT 0,
      total_investments_cents   INTEGER NOT NULL DEFAULT 0,
      total_assets_cents        INTEGER NOT NULL DEFAULT 0,
      total_liabilities_cents   INTEGER NOT NULL DEFAULT 0,
      net_worth_cents           INTEGER NOT NULL DEFAULT 0,
      household_net_worth_cents INTEGER,
      monthly_income_cents      INTEGER,
      monthly_expense_cents     INTEGER,
      computed_at               TEXT NOT NULL,
      as_of_date                TEXT NOT NULL CHECK (as_of_date IS date(as_of_date)),
      CHECK (net_worth_cents = total_assets_cents - total_liabilities_cents)
    )
    """,
    # ================================================================ derived credit
    # Design §4.6: `credit_exposure_cents` = Σ loan balances + Σ card limits, and per-card
    # `utilization_bps` = balance * 10000 / limit rolled up to a customer-level figure. Stored
    # alongside the exposure so the recompute job writes one row per customer.
    """
    CREATE TABLE derived_credit (
      customer_id            TEXT PRIMARY KEY REFERENCES customer(customer_id),
      credit_exposure_cents  INTEGER NOT NULL DEFAULT 0 CHECK (credit_exposure_cents >= 0),
      credit_utilization_bps INTEGER CHECK (credit_utilization_bps IS NULL OR
                                            credit_utilization_bps >= 0),
      total_credit_limit_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_credit_limit_cents >= 0),
      total_card_balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_card_balance_cents >= 0),
      computed_at            TEXT NOT NULL,
      as_of_date             TEXT NOT NULL CHECK (as_of_date IS date(as_of_date))
    )
    """,
    # ================================================================ derived health
    # Design §7.3: the fhs-v1 heuristic. `value` is 0..100, `band` is a labelled bucket,
    # `provenance` is always HEURISTIC (a computed score is never rendered with bureau-score
    # styling, D10), and `drivers` is the JSON array of named factor contributions the requirement
    # renders. `formula_version` is stored so a score computed under fhs-v1 is distinguishable from
    # one a later formula would produce.
    """
    CREATE TABLE derived_health (
      customer_id     TEXT PRIMARY KEY REFERENCES customer(customer_id),
      value           INTEGER NOT NULL CHECK (value BETWEEN 0 AND 100),
      band            TEXT NOT NULL CHECK (band IN
                        ('POOR','FAIR','GOOD','EXCELLENT')),
      provenance      TEXT NOT NULL DEFAULT 'HEURISTIC' CHECK (provenance = 'HEURISTIC'),
      formula_version TEXT NOT NULL,
      drivers         TEXT NOT NULL CHECK (json_valid(drivers)),
      computed_at     TEXT NOT NULL,
      as_of_date      TEXT NOT NULL CHECK (as_of_date IS date(as_of_date))
    )
    """,
    # ================================================================ customer search (FTS5)
    # Design §4.4. Identifier columns (account/loan numbers, card last-4) are denormalized into the
    # row so one MATCH query satisfies every search key in requirement 3.1; `customer_id` and
    # `segment` are UNINDEXED because they are carried for the join back and for display, not
    # searched on. `prefix = "2 3 4"` builds prefix indexes so typing part of a name or number
    # matches. Populated by the recompute job, never by the generator.
    """
    CREATE VIRTUAL TABLE customer_search USING fts5(
      customer_id UNINDEXED,
      customer_name, email, phone_number, mobile_number,
      account_numbers, loan_numbers, card_last4, city, segment UNINDEXED,
      tokenize = "unicode61 remove_diacritics 2",
      prefix = "2 3 4"
    )
    """,
    # ================================================================ graph nodes
    # Design §5.1. `node_id` is a typed composite key ("CUSTOMER:C00042") so a node is addressable
    # without a second lookup; `entity_id` is the bare source PK for the join back. The 16 node
    # types are the closed set the projection emits.
    """
    CREATE TABLE graph_node (
      node_id   TEXT PRIMARY KEY,
      node_type TEXT NOT NULL CHECK (node_type IN
                  ('Customer','Account','Loan','Deposit','Card','Investment','Property',
                   'Vehicle','Offer','Application','Transaction','Event','LifeEvent',
                   'Household','Organization','Employer')),
      entity_id TEXT NOT NULL,
      label     TEXT NOT NULL,
      props     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(props))
    )
    """,
    "CREATE INDEX ix_graph_node_type ON graph_node(node_type)",
    # ================================================================ graph edges
    # Design §5.1. The 15 edge types are the closed set. UNIQUE (src, dst, type) makes the
    # projection idempotent: rebuilding cannot double an edge. `is_inferred`/`confidence` carry the
    # same inferred-vs-system-of-record distinction the relational relationship tables do.
    """
    CREATE TABLE graph_edge (
      edge_id     INTEGER PRIMARY KEY AUTOINCREMENT,
      src_id      TEXT NOT NULL REFERENCES graph_node(node_id),
      dst_id      TEXT NOT NULL REFERENCES graph_node(node_id),
      edge_type   TEXT NOT NULL CHECK (edge_type IN
                    ('OWNS','APPLIED_FOR','RECEIVED_OFFER','ACCEPTED_OFFER','HAS_ACCOUNT',
                     'HAS_LOAN','HAS_CARD','HAS_INVESTMENT','HAS_PROPERTY','HAS_VEHICLE',
                     'WORKS_FOR','PART_OF_HOUSEHOLD','RELATED_TO','TRANSACTED_WITH',
                     'GENERATED_EVENT')),
      props       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(props)),
      is_inferred INTEGER NOT NULL DEFAULT 0 CHECK (is_inferred IN (0,1)),
      confidence  REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
      UNIQUE (src_id, dst_id, edge_type)
    )
    """,
    "CREATE INDEX ix_graph_edge_src ON graph_edge(src_id)",
    "CREATE INDEX ix_graph_edge_dst ON graph_edge(dst_id)",
    # ================================================================ graph adjacency
    # Design §5.2 — the key optimization. `src_id = :n OR dst_id = :n` cannot use an index, so each
    # logical edge is materialized OUT (from src) and IN (from dst). Traversal is then a single
    # indexed equality on `ix_adj_src`. `edge_id` references the logical edge so a hop can be traced
    # back to its source. No UNIQUE here: the same (src, dst, type) legitimately appears once per
    # direction.
    """
    CREATE TABLE graph_adjacency (
      src_id      TEXT NOT NULL,
      dst_id      TEXT NOT NULL,
      edge_type   TEXT NOT NULL,
      direction   TEXT NOT NULL CHECK (direction IN ('OUT','IN')),
      edge_id     INTEGER NOT NULL REFERENCES graph_edge(edge_id),
      is_inferred INTEGER NOT NULL DEFAULT 0 CHECK (is_inferred IN (0,1)),
      confidence  REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
    )
    """,
    "CREATE INDEX ix_adj_src ON graph_adjacency(src_id, edge_type)",
    # ================================================================ household subgraph cache
    # Design §5.2: "Household subgraphs are precomputed during projection since they are the most
    # frequent traversal." One row per (household, member node) so the household view is a single
    # indexed read rather than a recursive walk.
    """
    CREATE TABLE graph_household_subgraph (
      household_id TEXT NOT NULL REFERENCES household(household_id),
      node_id      TEXT NOT NULL REFERENCES graph_node(node_id),
      depth        INTEGER NOT NULL CHECK (depth >= 0),
      PRIMARY KEY (household_id, node_id)
    )
    """,
)

#: Child-before-parent. `graph_adjacency` and `graph_household_subgraph` reference `graph_edge` /
#: `graph_node`, so they drop first; the derived tables and the FTS5 table stand alone.
_DROP_ORDER: Statements = (
    "graph_household_subgraph",
    "graph_adjacency",
    "graph_edge",
    "graph_node",
    "customer_search",
    "derived_health",
    "derived_credit",
    "derived_financial",
)


def upgrade() -> None:
    execute_all(_UPGRADE)


def downgrade() -> None:
    drop_tables(_DROP_ORDER)
