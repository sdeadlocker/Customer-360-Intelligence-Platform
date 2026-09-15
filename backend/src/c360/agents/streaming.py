"""Server-sent-event streaming of the dashboard graph (task 8.9, design §8.1).

The dashboard is streamed so a fast card (financial health) reaches the UI without waiting on a slow
one (the summary, which is gated on every wave). LangGraph's ``astream_events`` emits an event as
each node finishes; this module filters those to the agent nodes and forwards each as one SSE
``agent`` event carrying that agent's :class:`AgentResult`. A terminal ``done`` event closes the
stream, and any per-node failure has already been contained by the graph's guard (task 8.6), so it
arrives as an ``AgentResult`` with ``degraded`` set or as an ``error`` event — never as a broken
stream.

The per-card timeout (``AGENT_TIMEOUT``) is the node's own ``asyncio.wait_for`` inside the graph
(task 8.6), so a straggling card degrades on its own without stalling or truncating the others'
events — the stream itself is not bounded by any single agent.

SSE framing is deliberately hand-rolled (``event:``/``data:`` lines terminated by a blank line)
rather than pulled from a dependency: it is a few lines, and keeping it here means the route is a
thin adapter over this generator.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from c360.agents.result import AgentResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

    from c360.agents.graph import DashboardGraph
    from c360.security.model import Principal

#: The langgraph event kind emitted when a node (a "chain") finishes.
_NODE_END = "on_chain_end"


async def stream_dashboard(
    graph: DashboardGraph,
    principal: Principal,
    customer_id: str,
    *,
    only: Collection[str] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames as each agent node of the dashboard graph completes (task 8.9).

    ``only`` restricts which agents are forwarded — ``/recommendations`` streams just the offer
    agent, ``/insights`` streams the rest — while the *whole* graph still runs once (the summary
    depends on the others regardless of what the caller wants forwarded). A single graph run backs
    both endpoints, so the fan-out work is never duplicated.
    """
    initial = {"principal": principal, "customer_id": customer_id}
    seen: set[str] = set()
    async for event in graph.compiled.astream_events(initial, version="v2"):
        if event.get("event") != _NODE_END:
            continue
        node = event.get("name", "")
        result = _agent_result(event)
        if result is None or node in seen:
            continue
        if only is not None and node not in only:
            continue
        seen.add(node)
        yield _frame("agent", _result_payload(result))
    yield _frame("done", {"agents": sorted(seen)})


def _agent_result(event: dict[str, Any]) -> AgentResult | None:
    """Extract the :class:`AgentResult` a node wrote, if this event is an agent node's completion.

    An agent node returns ``{"agent_outputs": {name: AgentResult}, ...}``; ``load_context`` and
    ``finalize`` return other shapes and are skipped by finding no ``AgentResult`` here.
    """
    output = (event.get("data") or {}).get("output")
    if not isinstance(output, dict):
        return None
    outputs = output.get("agent_outputs")
    if not isinstance(outputs, dict):
        return None
    for value in outputs.values():
        if isinstance(value, AgentResult):
            return value
    return None


def _result_payload(result: AgentResult) -> dict[str, Any]:
    """The client-facing JSON for one agent card: outputs plus the labelling metadata (§8.2)."""
    return {
        "agent": result.agent,
        "outputs": result.outputs.model_dump(mode="json"),
        "fact_citations": [c.model_dump(mode="json") for c in result.fact_citations],
        "passage_citations": [c.model_dump(mode="json") for c in result.passage_citations],
        "unavailable_inputs": list(result.unavailable_inputs),
        "confidence": result.confidence,
        "generated_at": result.generated_at.isoformat(),
        "model_id": result.model_id,
        "prompt_version": result.prompt_version,
        "cache_hit": result.cache_hit,
        "degraded": result.degraded,
    }


def _frame(event: str, data: dict[str, Any]) -> str:
    """One SSE frame: an ``event:`` line, a ``data:`` JSON line, and the terminating blank line."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


__all__ = ["stream_dashboard"]
