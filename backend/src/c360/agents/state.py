"""The LangGraph dashboard state and its channel reducers (task 8.6, design §8.1).

Parallel branches in a ``StateGraph`` all write back into one state, so any channel several nodes
write concurrently needs a *reducer* that merges their writes instead of the last writer clobbering
the rest. The wave-1 agents run in parallel and each appends its result, its citations and possibly
an error; without reducers the join would keep only one branch's contribution.

Channels and why each is reduced the way it is:

* ``agent_outputs`` — a ``{agent_name: AgentResult}`` map, merged key-wise (:func:`merge_outputs`).
  Each agent writes only its own key, so a dict-update merge is exactly right and never loses a
  sibling's output.
* ``citations`` / ``errors`` / ``unavailable_inputs`` — append-only lists, reduced with
  ``operator.add`` so every branch's items accumulate.
* ``customer_id`` / ``principal`` / ``facts`` / ``knowledge`` — written once by ``load_context``
  before any fan-out, so they take the default last-write channel and never see concurrent writers.
"""

from __future__ import annotations

import operator
from typing import TYPE_CHECKING, Annotated, TypedDict

# These names appear in `C360State`'s annotations, which LangGraph resolves at runtime via
# `get_type_hints`. They must therefore be importable at module scope, not merely under
# TYPE_CHECKING, or graph construction raises `NameError` on the forward reference.
from c360.agents.provider import PassageRef
from c360.agents.result import AgentError, AgentResult, Citation
from c360.security.model import Principal
from c360.tools.facts import FactTable

if TYPE_CHECKING:
    from collections.abc import Mapping


def merge_outputs(
    left: Mapping[str, AgentResult], right: Mapping[str, AgentResult]
) -> dict[str, AgentResult]:
    """Key-wise merge of two ``{agent: AgentResult}`` maps (the ``agent_outputs`` reducer).

    Right wins on a key collision, which never happens in normal operation — each agent owns its own
    key — but is defined so a re-run of a node (LangGraph may retry) overwrites, not duplicates.
    """
    return {**left, **right}


class C360State(TypedDict, total=False):
    """The dashboard graph's state (design §8.1).

    ``total=False`` because ``load_context`` populates the input channels and the agent nodes fill
    the output channels incrementally; a node reads what it needs and writes only its own channels.
    """

    customer_id: str
    principal: Principal
    #: Per-agent fact tables built once by ``load_context`` (design §8.4). Keyed by agent name.
    facts: dict[str, FactTable]
    #: Per-agent retrieved passages, present only for the three retrieving agents, by agent name.
    knowledge: dict[str, tuple[PassageRef, ...]]
    agent_outputs: Annotated[dict[str, AgentResult], merge_outputs]
    citations: Annotated[list[Citation], operator.add]
    errors: Annotated[list[AgentError], operator.add]
    unavailable_inputs: Annotated[list[str], operator.add]


__all__ = ["C360State", "merge_outputs"]
