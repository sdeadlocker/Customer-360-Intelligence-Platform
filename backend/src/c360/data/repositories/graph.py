"""SQLite adapter for :class:`~c360.domain.ports.GraphRepository` (task 5.3, design §5.2, §5.3).

Traversal is a recursive CTE over ``graph_adjacency`` — the table where every logical edge is
materialized OUT and IN so a hop is a single indexed equality on ``ix_adj_src (src_id, edge_type)``
rather than the ``src_id = :n OR dst_id = :n`` scan a naive graph query would force (design §5.2).
SQLite has no arrays, so the visited-set that stops a cycle is a delimited ``TEXT`` path guarded
with
``instr`` — the exact shape design §5.2 specifies.

Every read is bounded. ``max_hops`` caps depth inside the recursion and ``node_cap`` caps the
result,
so a hub customer with thousands of edges cannot turn a neighborhood into a table scan and miss the
two-second budget (requirement 6.7). The caps are passed in by the service from configuration
(``GRAPH_MAX_HOPS``, ``GRAPH_NODE_CAP``) rather than read here, so a test can pin them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from c360.core.telemetry import SpanAttr, get_metrics, get_tracer
from c360.data.repositories.base import SqliteRepository, Statement
from c360.domain.graph import GraphEdge, GraphNode, GraphView

_TRACER_NAME = "c360.graph"

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Row

# Neighborhood traversal (design §5.2). The walk carries a delimited path so `instr` is the
# visited-set guard; `MIN(depth)` collapses multiple routes to a node to its shortest, and the
# result is capped. `:edge_types` is a JSON array or NULL — NULL means "any edge type" — decoded
# with `json_each` so a variable-length filter needs no generated placeholders.
_NEIGHBORHOOD = Statement(
    id="graph.neighborhood",
    collection="graph_adjacency",
    sql="""
    WITH RECURSIVE walk(node_id, depth, path) AS (
      SELECT :root, 0, '|' || :root || '|'
      UNION ALL
      SELECT a.dst_id, w.depth + 1, w.path || a.dst_id || '|'
      FROM walk w
      JOIN graph_adjacency a ON a.src_id = w.node_id
      WHERE w.depth < :max_hops
        AND instr(w.path, '|' || a.dst_id || '|') = 0
        AND (:edge_types IS NULL OR a.edge_type IN (SELECT value FROM json_each(:edge_types)))
    )
    SELECT node_id, MIN(depth) AS depth
    FROM walk
    GROUP BY node_id
    ORDER BY depth, node_id
    LIMIT :node_cap
    """,
)

# Shortest path. Same walk, but it keeps the whole path string and stops at the first time `target`
# is reached; ordering by depth then returning the top row yields a shortest path. The hop cap keeps
# an unreachable target from walking the whole component.
_PATH_BETWEEN = Statement(
    id="graph.path_between",
    collection="graph_adjacency",
    sql="""
    WITH RECURSIVE walk(node_id, depth, path) AS (
      SELECT :source, 0, '|' || :source || '|'
      UNION ALL
      SELECT a.dst_id, w.depth + 1, w.path || a.dst_id || '|'
      FROM walk w
      JOIN graph_adjacency a ON a.src_id = w.node_id
      WHERE w.depth < :max_hops
        AND instr(w.path, '|' || a.dst_id || '|') = 0
    )
    SELECT path
    FROM walk
    WHERE node_id = :target
    ORDER BY depth
    LIMIT 1
    """,
)

_DEGREE = Statement(
    id="graph.degree",
    collection="graph_adjacency",
    # Degree is the count of incident edges. Each logical edge is materialized once OUT and once IN,
    # so counting `graph_edge` rows touching the node (either endpoint) is the undirected degree
    # without double-counting the adjacency mirror.
    sql="""
    SELECT COUNT(*) FROM graph_edge WHERE src_id = :root OR dst_id = :root
    """,
)

_HOUSEHOLD_NODES = Statement(
    id="graph.household_nodes",
    collection="graph_household_subgraph",
    sql="""
    SELECT node_id FROM graph_household_subgraph WHERE household_id = :household_id
    ORDER BY depth, node_id
    """,
)


def _nodes_statement(node_ids: Sequence[str]) -> tuple[Statement, dict[str, Any]]:
    """Fetch node rows for a set of ids. Built per call because the id count varies."""
    names = [f"n{index}" for index in range(len(node_ids))]
    placeholders = ", ".join(f":{name}" for name in names)
    params = dict(zip(names, node_ids, strict=True))
    return (
        Statement(
            id="graph.nodes",
            collection="graph_node",
            sql=(
                "SELECT node_id, node_type, entity_id, label, props "  # noqa: S608 - names only
                f"FROM graph_node WHERE node_id IN ({placeholders})"
            ),
        ),
        params,
    )


def _edges_statement(node_ids: Sequence[str]) -> tuple[Statement, dict[str, Any]]:
    """Fetch edges whose *both* endpoints are in the node set, so the view is self-contained."""
    names = [f"n{index}" for index in range(len(node_ids))]
    placeholders = ", ".join(f":{name}" for name in names)
    params = dict(zip(names, node_ids, strict=True))
    return (
        Statement(
            id="graph.edges",
            collection="graph_edge",
            sql=(
                "SELECT src_id, dst_id, edge_type, props, is_inferred, confidence "  # noqa: S608
                f"FROM graph_edge WHERE src_id IN ({placeholders}) AND dst_id IN ({placeholders})"
            ),
        ),
        params,
    )


class SqliteGraphRepository(SqliteRepository):
    """Recursive-CTE traversal over the projected graph (design §5.2, §5.3)."""

    def neighborhood(
        self,
        root: str,
        *,
        max_hops: int,
        node_cap: int,
        edge_types: frozenset[str] | None = None,
    ) -> GraphView:
        if max_hops < 1:
            raise ValueError(f"max_hops must be at least 1, got {max_hops}")
        if node_cap < 1:
            raise ValueError(f"node_cap must be at least 1, got {node_cap}")
        # A span with the traversal shape (task 10.1): hops, nodes visited and whether the node cap
        # truncated the walk. These attributes are on the allowlist; setting them here is what fills
        # the graph gap the allowlist reserved a place for. Only the shape is recorded — no node id,
        # which would be a customer reference (design §13.4).
        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span("graph.neighborhood") as span:
            span.set_attribute(SpanAttr.GRAPH_HOPS, max_hops)
            rows = self.fetch_all(
                _NEIGHBORHOOD,
                {
                    "root": root,
                    "max_hops": max_hops,
                    "node_cap": node_cap,
                    "edge_types": json.dumps(sorted(edge_types)) if edge_types else None,
                },
            )
            node_ids = tuple(str(row[0]) for row in rows)
            truncated = len(node_ids) >= node_cap
            span.set_attribute(SpanAttr.GRAPH_NODES_VISITED, len(node_ids))
            span.set_attribute(SpanAttr.GRAPH_TRUNCATED, truncated)
            metrics = get_metrics()
            if metrics is not None:
                metrics.record_graph(
                    hops=max_hops, nodes_visited=len(node_ids), truncated=truncated
                )
            return self._build_view(node_ids)

    def path_between(self, source: str, target: str, *, max_hops: int) -> tuple[str, ...]:
        if max_hops < 1:
            raise ValueError(f"max_hops must be at least 1, got {max_hops}")
        if source == target:
            return (source,)
        path = self.fetch_scalar(
            _PATH_BETWEEN, {"source": source, "target": target, "max_hops": max_hops}
        )
        if path is None:
            return ()
        # The stored form is '|A|B|C|'; strip the delimiters back into an ordered tuple.
        return tuple(part for part in str(path).split("|") if part)

    def household_subgraph(self, household_id: str) -> GraphView:
        rows = self.fetch_all(_HOUSEHOLD_NODES, {"household_id": household_id})
        node_ids = tuple(str(row[0]) for row in rows)
        return self._build_view(node_ids)

    def degree_centrality(self, root: str) -> int:
        return int(self.fetch_scalar(_DEGREE, {"root": root}, default=0))

    # ---------------------------------------------------------------- view assembly
    def _build_view(self, node_ids: Sequence[str]) -> GraphView:
        """Load the nodes and the edges internal to them into a :class:`GraphView`."""
        if not node_ids:
            return GraphView()
        node_stmt, node_params = _nodes_statement(node_ids)
        nodes = tuple(_to_node(row) for row in self.fetch_all(node_stmt, node_params))
        edge_stmt, edge_params = _edges_statement(node_ids)
        edges = tuple(_to_edge(row) for row in self.fetch_all(edge_stmt, edge_params))
        return GraphView(nodes=nodes, edges=edges)


def _to_node(row: Row[Any]) -> GraphNode:
    data = dict(row._mapping)
    return GraphNode(
        node_id=str(data["node_id"]),
        node_type=str(data["node_type"]),
        entity_id=str(data["entity_id"]),
        label=str(data["label"]),
        props=json.loads(data["props"]) if data.get("props") else {},
    )


def _to_edge(row: Row[Any]) -> GraphEdge:
    data = dict(row._mapping)
    return GraphEdge(
        src_id=str(data["src_id"]),
        dst_id=str(data["dst_id"]),
        edge_type=str(data["edge_type"]),
        props=json.loads(data["props"]) if data.get("props") else {},
        is_inferred=bool(data["is_inferred"]),
        confidence=data.get("confidence"),
    )
