"""Process-wide agent runtime and its lazy graph build (tasks 8.8, 8.9).

The dashboard graph needs the service container, which is built lazily on the first request (a bare
``create_app`` in a test must not open the database). So the *stateless* pieces — the LLM provider,
the versioned prompt registry and the shared output cache — are built once at app construction and
held in :class:`AgentRuntime`, and the graph itself is built the first time it is needed and cached,
double-checked-locked exactly as the service container is.

Keeping the cache on the runtime (and thus reachable from ``app.state``) is what lets the admin
recompute endpoint invalidate it (design §8.7): a recompute can change any customer's facts, so the
whole cache is dropped rather than trying to figure out which narratives a projection touched.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from c360.agents.breaker import CircuitBreaker
from c360.agents.cache import AgentCache
from c360.agents.graph import DashboardGraph
from c360.agents.prompts import build_prompt_registry
from c360.agents.provider import build_llm_provider
from c360.agents.qa_runtime import QaMemory
from c360.core.telemetry import breaker_transition_hook
from c360.tools import build_tool_registry

if TYPE_CHECKING:
    from c360.agents.prompts import PromptRegistry
    from c360.agents.provider import LLMProvider
    from c360.agents.qa_graph import QaGraph
    from c360.api.services import Services
    from c360.core.config import Settings
    from c360.domain.ports import AuditSink
    from c360.tools.registry import ToolRegistry


class AgentRuntime:
    """The stateless agent dependencies, plus a lazily-built, cached graph (tasks 8.8, 8.9).

    One instance per process, stored on ``app.state.agent_runtime``. The provider, prompt registry,
    tool registry and output cache are built eagerly (they need no database); the graph is built on
    first use because it binds the lazily-built service container.
    """

    __slots__ = (
        "_graph",
        "_lock",
        "_max_tool_calls",
        "_qa_graph",
        "_qa_lock",
        "_wave_budgets_s",
        "breaker",
        "cache",
        "memory",
        "prompts",
        "provider",
        "registry",
    )

    def __init__(self, settings: Settings) -> None:
        self.provider: LLMProvider = build_llm_provider(settings)
        self.prompts: PromptRegistry = build_prompt_registry(settings)
        self.registry: ToolRegistry = build_tool_registry()
        self.cache = AgentCache(ttl_s=settings.agent_cache_ttl_s)
        # One process-wide generation breaker: consecutive Bedrock failures across any card trip it,
        # after which cards fall back to the deterministic template until Bedrock recovers (§13.7).
        self.breaker = CircuitBreaker("generation", on_transition=breaker_transition_hook)
        self._graph: DashboardGraph | None = None
        self._lock = threading.Lock()
        # Per-wave timeout budgets (design §8.1), configurable so a live-Bedrock demo can relax the
        # tight defaults that assume warmed, in-region latency (task 8.6).
        self._wave_budgets_s = {
            1: settings.agent_wave1_budget_s,
            2: settings.agent_wave2_budget_s,
            3: settings.agent_wave3_budget_s,
        }
        # Q&A (Phase 9): the tool-call cap (task 9.1) and the conversation-memory holder (task 9.2).
        # The Q&A graph is built on the running event loop the first time it is needed, because its
        # checkpointer binds an aiosqlite connection to that loop; hence an asyncio lock, separate
        # from the threading lock guarding the dashboard graph.
        self._max_tool_calls = settings.qa_max_tool_calls
        self.memory = QaMemory(settings.checkpoint_db_path)
        self._qa_graph: QaGraph | None = None
        self._qa_lock = asyncio.Lock()

    def graph_for(
        self, services: Services, *, audit_sink: AuditSink | None = None
    ) -> DashboardGraph:
        """The dashboard graph bound to ``services``, built once and cached.

        Double-checked locking mirrors the service container's own lazy build so two concurrent
        first requests do not each compile a graph. The audit sink is process-wide (one writer), so
        it is bound at the first build and reused for every request thereafter.
        """
        if self._graph is not None:
            return self._graph
        with self._lock:
            if self._graph is None:
                self._graph = DashboardGraph(
                    registry=self.registry,
                    services=services,
                    provider=self.provider,
                    prompts=self.prompts,
                    cache=self.cache,
                    audit_sink=audit_sink,
                    breaker=self.breaker,
                    wave_budgets_s=self._wave_budgets_s,
                )
            return self._graph

    async def qa_graph_for(self, services: Services) -> QaGraph:
        """The Q&A ReAct graph bound to ``services``, compiled with the memory checkpointer (9.2).

        Built on the running event loop the first time it is needed so the checkpointer's aiosqlite
        connection is created on that loop. Double-checked under an asyncio lock so two concurrent
        first Q&A requests do not each compile a graph.
        """
        if self._qa_graph is not None:
            return self._qa_graph
        async with self._qa_lock:
            if self._qa_graph is None:
                from c360.agents.qa_graph import QaGraph  # noqa: PLC0415 - avoid import cycle

                checkpointer = await self.memory.checkpointer()
                self._qa_graph = QaGraph(
                    registry=self.registry,
                    services=services,
                    provider=self.provider,
                    prompts=self.prompts,
                    max_tool_calls=self._max_tool_calls,
                    checkpointer=checkpointer,
                    # Share the process-wide generation breaker with the dashboard graph so a
                    # sustained Bedrock outage detected on either surface trips one breaker, and
                    # the Q&A loop fails fast to its grounded mock fallback (§13.7).
                    breaker=self.breaker,
                )
            return self._qa_graph


def build_agent_runtime(settings: Settings) -> AgentRuntime:
    """Construct the process-wide agent runtime (called once at app construction)."""
    return AgentRuntime(settings)


__all__ = ["AgentRuntime", "build_agent_runtime"]
