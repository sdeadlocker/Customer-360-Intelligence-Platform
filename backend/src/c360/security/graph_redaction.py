"""Graph redaction filter (task 4.7, design §5.4, requirement 6.4).

A relationship graph naturally reaches customers outside the caller's book — a household member, a
referral, a joint-account party. Requirement 6.4 is precise about the result: such a node is
rendered **present-but-restricted**, showing the relationship type only and no identifying detail.
The edge stays; the node's identity goes.

This lives in the service layer, not in a route handler, because two consumers need identical
behaviour: the UI's relationship view and the Q&A agent traversing the same graph (design §5.4). A
redaction applied in one and forgotten in the other would leak through whichever path skipped it, so
there is exactly one implementation and both call it.

What "restricted" means concretely
-----------------------------------

A non-entitled ``Customer`` node is replaced by one whose ``props`` is ``{"restricted": true,
"edge_type_only": true}``, whose ``label`` is a fixed placeholder, and whose ``entity_id`` is
cleared — so nothing about the real customer (name, id, segment) survives, while the node still
exists as an anchor for its edges. Every other node type passes through untouched: only a customer
node can belong to someone outside the book, and only it carries identity worth withholding.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Final

from c360.domain.graph import CUSTOMER_NODE_TYPE, GraphNode, GraphView

#: The props of a restricted node. Exactly the design §5.4 shape.
_RESTRICTED_PROPS: Final = {"restricted": True, "edge_type_only": True}

#: The label a restricted node shows in place of the customer's name.
_RESTRICTED_LABEL: Final = "Restricted"

#: A predicate answering "may this principal see this customer entity_id?". The service supplies one
#: closed over the customer repository (via :func:`c360.security.authorization.is_customer_visible`)
#: so the graph filter needs no repository of its own and stays a pure transform.
VisibilityCheck = Callable[[str], bool]


def _restricted_node_id(node_id: str) -> str:
    """A stable, opaque anchor id for a restricted node.

    The real ``node_id`` embeds the customer's source id (``CUSTOMER:C00042``), so leaving it in
    place would let a caller confirm a specific non-entitled customer exists in the graph — the same
    existence-oracle the 403-versus-404 rule guards against (requirement 15.5). The replacement is a
    deterministic hash of the original, so every edge that referenced the node still lines up on one
    consistent anchor, but the anchor reveals nothing.
    """
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    return f"CUSTOMER:restricted-{digest}"


def redact_node(node: GraphNode, is_visible: VisibilityCheck) -> GraphNode:
    """Return ``node`` unchanged, or its restricted form if it is a non-entitled customer.

    Non-customer nodes are always returned as-is: they are structure, not identity. A customer node
    is returned as-is when ``is_visible(entity_id)`` is true and reduced to a structure-only node
    with an opaque anchor id otherwise.
    """
    if node.node_type != CUSTOMER_NODE_TYPE:
        return node
    if is_visible(node.entity_id):
        return node
    return GraphNode(
        node_id=_restricted_node_id(node.node_id),
        node_type=CUSTOMER_NODE_TYPE,
        entity_id="",
        label=_RESTRICTED_LABEL,
        props=dict(_RESTRICTED_PROPS),
    )


def redact_view(view: GraphView, is_visible: VisibilityCheck) -> GraphView:
    """Redact every non-entitled customer node in ``view``, preserving the relationship structure.

    Requirement 6.4 keeps the *relationship* visible even when the counterparty is not, so every
    edge survives — but an edge's endpoints are rewritten to the restricted anchor id of any node
    that was redacted, so the edge still resolves to a node in the view and no real customer id
    leaks through an edge endpoint either.
    """
    id_remap: dict[str, str] = {}
    redacted_nodes: list[GraphNode] = []
    for node in view.nodes:
        redacted = redact_node(node, is_visible)
        if redacted.node_id != node.node_id:
            id_remap[node.node_id] = redacted.node_id
        redacted_nodes.append(redacted)

    if not id_remap:
        return GraphView(nodes=tuple(redacted_nodes), edges=view.edges)

    redacted_edges = tuple(
        edge.model_copy(
            update={
                "src_id": id_remap.get(edge.src_id, edge.src_id),
                "dst_id": id_remap.get(edge.dst_id, edge.dst_id),
            }
        )
        for edge in view.edges
    )
    return GraphView(nodes=tuple(redacted_nodes), edges=redacted_edges)


def is_node_restricted(node: GraphNode) -> bool:
    """Whether ``node`` is a restricted placeholder — the UI's cue to render structure only."""
    return node.node_type == CUSTOMER_NODE_TYPE and node.props.get("restricted") is True


def visibility_check_for(
    principal_scope_permits: Callable[[str], bool],
) -> VisibilityCheck:
    """Adapt an entitlement-scope ``permits``-style callable into a :data:`VisibilityCheck`.

    A thin convenience so a caller that already has ``principal.entitlement.permits`` (the in-memory
    check, no database round trip) can pass it directly, while a caller that needs the
    exists-and-entitled database check passes a closure over
    :func:`c360.security.authorization.is_customer_visible` instead.
    """
    return principal_scope_permits


__all__ = [
    "VisibilityCheck",
    "is_node_restricted",
    "redact_node",
    "redact_view",
    "visibility_check_for",
]
