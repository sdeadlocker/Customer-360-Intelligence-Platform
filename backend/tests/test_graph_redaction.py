"""Task 4.7: the graph redaction filter for non-entitled customer nodes (requirement 6.4)."""

from __future__ import annotations

import json

from c360.domain.graph import GraphEdge, GraphNode, GraphView
from c360.security.entitlement import BookScope
from c360.security.graph_redaction import (
    is_node_restricted,
    redact_node,
    redact_view,
)


def _customer_node(entity_id: str, name: str) -> GraphNode:
    return GraphNode(
        node_id=f"CUSTOMER:{entity_id}",
        node_type="Customer",
        entity_id=entity_id,
        label=name,
        props={"segment": "AFFLUENT"},
    )


def _account_node() -> GraphNode:
    return GraphNode(
        node_id="ACCOUNT:A-1",
        node_type="Account",
        entity_id="A-1",
        label="Everyday Checking",
        props={"account_type": "DEPOSIT"},
    )


class TestRedactNode:
    def test_an_entitled_customer_node_is_unchanged(self) -> None:
        node = _customer_node("C-0001", "Renata Alvarez")
        result = redact_node(node, lambda cid: cid == "C-0001")
        assert result == node

    def test_a_non_entitled_customer_node_is_reduced_to_structure(self) -> None:
        node = _customer_node("C-0002", "Tomas Alvarez")
        result = redact_node(node, lambda cid: cid == "C-0001")
        # An opaque anchor, not the real node id: it must not confirm which customer this was.
        assert result.node_id.startswith("CUSTOMER:restricted-")
        assert "C-0002" not in result.node_id
        assert result.entity_id == ""
        assert result.label == "Restricted"
        assert result.props == {"restricted": True, "edge_type_only": True}
        # No identifying detail survives.
        assert "Tomas" not in result.label
        assert "segment" not in result.props

    def test_the_restricted_anchor_is_stable(self) -> None:
        node = _customer_node("C-0002", "Tomas Alvarez")
        first = redact_node(node, lambda _cid: False)
        second = redact_node(node, lambda _cid: False)
        assert first.node_id == second.node_id

    def test_a_non_customer_node_is_never_redacted(self) -> None:
        node = _account_node()
        result = redact_node(node, lambda _cid: False)
        assert result == node


class TestRedactView:
    def test_edges_are_preserved_and_the_counterparty_is_restricted(self) -> None:
        me = _customer_node("C-0001", "Renata Alvarez")
        them = _customer_node("C-0002", "Tomas Alvarez")
        edge = GraphEdge(
            src_id=me.node_id,
            dst_id=them.node_id,
            edge_type="RELATED_TO",
            props={"relationship": "SPOUSE"},
        )
        view = GraphView(nodes=(me, them), edges=(edge,))

        redacted = redact_view(view, lambda cid: cid == "C-0001")

        by_id = {node.node_id: node for node in redacted.nodes}
        assert by_id["CUSTOMER:C-0001"].label == "Renata Alvarez"
        restricted = next(n for n in redacted.nodes if is_node_restricted(n))
        # The relationship itself is intact — the edge type survives and the edge now points at the
        # restricted anchor rather than the real node id.
        assert len(redacted.edges) == 1
        assert redacted.edges[0].edge_type == "RELATED_TO"
        assert redacted.edges[0].dst_id == restricted.node_id

    def test_no_restricted_name_appears_anywhere_in_the_serialized_view(self) -> None:
        me = _customer_node("C-0001", "Renata Alvarez")
        them = GraphNode(
            node_id="CUSTOMER:C-0002",
            node_type="Customer",
            entity_id="C-0002",
            label="Tomas Alvarez",
            props={"segment": "UHNW"},  # a distinctive value only the restricted node carries
        )
        view = GraphView(
            nodes=(me, them),
            edges=(GraphEdge(src_id=me.node_id, dst_id=them.node_id, edge_type="RELATED_TO"),),
        )
        redacted = redact_view(view, lambda cid: cid == "C-0001")
        blob = json.dumps(redacted.model_dump())
        # Nothing about the restricted customer survives: not the name, not the segment, not even
        # the source id in a node id or an edge endpoint.
        assert "Tomas Alvarez" not in blob
        assert "UHNW" not in blob
        assert "C-0002" not in blob
        # ...and the entitled customer is still fully present.
        assert "Renata Alvarez" in blob

    def test_works_with_an_entitlement_scope_permits(self) -> None:
        """The scope's own in-memory permits() plugs straight in as the visibility check."""
        scope = BookScope(customer_ids=frozenset({"C-0001"}))
        me = _customer_node("C-0001", "Renata Alvarez")
        them = _customer_node("C-0099", "Outsider")
        view = GraphView(nodes=(me, them), edges=())
        redacted = redact_view(view, scope.permits)
        entitled = next(n for n in redacted.nodes if n.node_id == "CUSTOMER:C-0001")
        assert not is_node_restricted(entitled)
        assert any(is_node_restricted(n) for n in redacted.nodes)
        # The outsider's id never appears in the redacted view.
        assert all("C-0099" not in n.node_id for n in redacted.nodes)
