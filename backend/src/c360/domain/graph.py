"""Graph projection read models (design §5.1).

The relationship graph is projected into ``graph_node`` / ``graph_edge`` in Phase 3; these are the
shapes a reader of that projection returns. They are intentionally thin — a node is a typed id, a
label and a free-form ``props`` bag — because the graph's value is the *structure*, and the typed
customer facts still come from the customer repositories, never from a node's props.

The graph traversal repository and service arrive in Phase 5. The models live here now because the
graph **redaction filter** (task 4.7) operates on them: a non-entitled customer node must be reduced
to structure-only before it reaches the UI or the Q&A agent, and that filter is written against
these types so both consumers share one implementation.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import ConfigDict, Field

from c360.domain.models import DomainModel

#: The one node type whose visibility is governed by entitlement. Every other node type
#: (accounts, offers, employers, …) is reachable structure; only a *customer* node can belong to
#: someone outside the caller's book, and only it carries identifying detail worth redacting.
CUSTOMER_NODE_TYPE: Final = "Customer"


class GraphNode(DomainModel):
    """One projected graph node.

    ``props`` is a decoded JSON object rather than a string: the projection stores it as text, but a
    reader hands back the parsed form so a consumer — including the redaction filter — works with a
    mapping, not a blob.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    node_type: str
    entity_id: str
    label: str
    props: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(DomainModel):
    """One projected graph edge, in its stored direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    src_id: str
    dst_id: str
    edge_type: str
    props: dict[str, Any] = Field(default_factory=dict)
    is_inferred: bool = False
    confidence: float | None = None


class GraphView(DomainModel):
    """A neighbourhood or subgraph: nodes plus the edges among them.

    The shape a traversal returns and the shape the redaction filter consumes and re-emits. Edges
    are preserved through redaction; only node contents change, so the structure a user is allowed
    to *see the shape of* survives even where the node's identity does not (requirement 6.4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
