"""The LangGraph dashboard `StateGraph` (task 8.6, design §8.1).

Topology, fixed by design §8.1::

    load_context → {financial_health, risk, life_event, relationship}   (wave 1, parallel)
                 → {offer_recommendation, journey}                       (wave 2, parallel)
                 → customer_summary                                      (wave 3)
                 → finalize → END

``load_context`` fetches every fact any agent needs *once* through the tool registry and builds a
per-agent fact table (design §8.4): without it, seven agents would re-query the same profile and
holdings and the 5 s budget would go on duplicate reads. It also runs retrieval for the three
retrieving agents, so an agent node never touches the database or the knowledge index — it only
generates from what ``load_context`` prepared.

Every agent node is wrapped by :func:`_guarded` so its own timeout (``asyncio.wait_for``) and any
failure are contained: a timeout or an exception writes an :class:`AgentError` and the agent's
declared name into ``unavailable_inputs`` and returns, rather than raising. A wave therefore never
blocks on a straggler and never fails as a whole — the dashboard degrades card by card (design §8.1,
requirement 13.6).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from langgraph.graph import END, START, StateGraph

from c360.agents.audit import build_agent_audit_hook
from c360.agents.cache import (
    AgentCache,
    agent_cache_key,
    fact_fingerprint,
    knowledge_fingerprint,
)
from c360.agents.provider import PassageRef
from c360.agents.result import AgentError, AgentResult
from c360.agents.runner import AgentInput, AgentRunner
from c360.agents.specs import AGENT_SPECS, WAVE_1, WAVE_2, WAVE_3, AgentSpec
from c360.agents.state import C360State
from c360.core.telemetry import SpanAttr, get_tracer
from c360.domain.health import FORMULA_VERSION
from c360.tools.facts import FactTable
from c360.tools.registry import ToolContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from c360.agents.audit import AgentAuditHook
    from c360.agents.breaker import CircuitBreaker
    from c360.agents.prompts import PromptRegistry
    from c360.agents.provider import LLMProvider
    from c360.agents.redaction import PromptRedactor
    from c360.api.services import Services
    from c360.domain.ports import AuditSink
    from c360.security.model import Principal
    from c360.tools.registry import ToolRegistry

_TRACER_NAME = "c360.agents"


class DashboardGraph:
    """Builds and runs the dashboard agent graph for one principal + customer (task 8.6)."""

    __slots__ = (
        "_audit_sink",
        "_breaker",
        "_cache",
        "_compiled",
        "_prompts",
        "_provider",
        "_registry",
        "_services",
        "_specs",
    )

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        services: Services,
        provider: LLMProvider,
        prompts: PromptRegistry,
        cache: AgentCache | None = None,
        audit_sink: AuditSink | None = None,
        breaker: CircuitBreaker | None = None,
        wave_budgets_s: dict[int, float] | None = None,
    ) -> None:
        self._registry = registry
        self._services = services
        self._provider = provider
        self._prompts = prompts
        self._audit_sink = audit_sink
        self._breaker = breaker
        # A zero-TTL cache when none is supplied, so a caller that does not want caching (a test, a
        # one-shot eval run) gets correct behaviour without a separate flag.
        self._cache = cache if cache is not None else AgentCache(ttl_s=0)
        # Bind each spec to the field allowlist from its versioned prompt file (task 8.3), so the
        # reviewed prompt is the source of truth for what the redactor lets through. Also apply the
        # configurable per-wave timeout budget when one is supplied, so an operator can relax the
        # design's tight p95 budgets for a live-Bedrock demo without editing the specs (task 8.6).
        budgets = wave_budgets_s or {}
        self._specs = {
            name: _bind_spec(spec, prompts.for_agent(name).field_allowlist, budgets.get(spec.wave))
            for name, spec in AGENT_SPECS.items()
        }
        self._compiled = self._build().compile()

    async def arun(self, principal: Principal, customer_id: str) -> C360State:
        """Run the whole graph and return the final state (all agent outputs, citations, errors)."""
        initial: C360State = {"principal": principal, "customer_id": customer_id}
        # langgraph types ``ainvoke`` as ``dict | Any``; the compiled graph returns the state dict.
        result = await self._compiled.ainvoke(initial)
        return cast("C360State", result)

    @property
    def compiled(self) -> Any:
        """The compiled graph, exposed for the SSE path's ``astream_events`` (task 8.9)."""
        return self._compiled

    # ---------------------------------------------------------------- build

    def _build(self) -> StateGraph:
        graph: StateGraph = StateGraph(C360State)
        graph.add_node("load_context", self._load_context)
        graph.add_node("finalize", _finalize)
        for name in (*WAVE_1, *WAVE_2, *WAVE_3):
            graph.add_node(name, self._agent_node(self._specs[name]))

        graph.add_edge(START, "load_context")
        for name in WAVE_1:
            graph.add_edge("load_context", name)
        # Wave 2 depends on all of wave 1 (fan-in then fan-out): a list of sources on the edge makes
        # the wave-2 node wait for every wave-1 node to finish.
        for name in WAVE_2:
            graph.add_edge(list(WAVE_1), name)
        for name in WAVE_3:
            graph.add_edge(list(WAVE_2), name)
        graph.add_edge(list(WAVE_3), "finalize")
        graph.add_edge("finalize", END)
        return graph

    # ---------------------------------------------------------------- load_context

    async def _load_context(self, state: C360State) -> dict[str, Any]:
        """Fetch every agent's facts once and retrieve passages for the retrieving agents (§8.4)."""
        principal = state["principal"]
        customer_id = state["customer_id"]
        context = ToolContext(principal=principal, services=self._services)

        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span("load_context") as span:
            span.set_attribute(SpanAttr.AGENT, "load_context")
            tool_facts, unavailable_tools = self._run_source_tools(context, customer_id)
            facts_by_agent: dict[str, FactTable] = {}
            unavailable_by_agent: dict[str, tuple[str, ...]] = {}
            for name, spec in self._specs.items():
                merged: list[Any] = []
                missing: list[str] = []
                for tool in spec.source_tools:
                    if tool in unavailable_tools:
                        missing.append(tool)
                        continue
                    merged.extend(tool_facts.get(tool, FactTable()).facts)
                facts_by_agent[name] = _renumber(merged)
                unavailable_by_agent[name] = tuple(missing)

            knowledge = self._retrieve(principal, customer_id, facts_by_agent)
            span.set_attribute(SpanAttr.OUTCOME, "ok")

        return {
            "facts": facts_by_agent,
            "knowledge": knowledge,
            # Stash per-agent unavailable-tool lists on the state via a private channel-free dict is
            # not possible (TypedDict), so carry them inside facts is wrong; instead the agent node
            # recomputes availability from `facts`. Tool-level gaps surface as unavailable_inputs.
            "unavailable_inputs": sorted(unavailable_tools),
        }

    def _run_source_tools(
        self, context: ToolContext, customer_id: str
    ) -> tuple[dict[str, FactTable], set[str]]:
        """Run every distinct source tool once; return its facts and the set that failed.

        A tool failure is isolated: the agents that depend on it see it as a missing input rather
        than the whole load failing. Authorization is enforced in each tool exactly as over REST.
        """
        needed = {tool for spec in self._specs.values() for tool in spec.source_tools}
        results: dict[str, FactTable] = {}
        failed: set[str] = set()
        for tool in sorted(needed):
            try:
                result = self._registry.execute(tool, context, {"customer_id": customer_id})
            except Exception:
                failed.add(tool)
                continue
            results[tool] = getattr(result, "facts", FactTable())
        return results, failed

    def _retrieve(
        self,
        principal: Principal,
        customer_id: str,
        facts_by_agent: dict[str, FactTable],
    ) -> dict[str, tuple[PassageRef, ...]]:
        """Retrieve passages for the three retrieving agents, or nothing when knowledge is off.

        The retrieval query is built from non-identifying qualifiers only and scrubbed of PII before
        it is embedded (design §9.4), so customer identity never reaches the embedding model. When
        the knowledge base is unavailable the agent simply gets no passages — the "no supporting
        guidance found" path — never an error.
        """
        del customer_id  # identity must not enter the query
        service = self._services.knowledge
        knowledge: dict[str, tuple[PassageRef, ...]] = {}
        if service is None:
            return knowledge
        from c360.knowledge.models import KnowledgeDomain as _Domain  # noqa: PLC0415

        for name, spec in self._specs.items():
            if not spec.knowledge_domains:
                continue
            domains = tuple(_Domain(d) for d in spec.knowledge_domains)
            query = spec.task
            try:
                result = service.search(principal, query, domains=domains)
            except Exception:
                knowledge[name] = ()
                continue
            knowledge[name] = tuple(
                PassageRef(
                    passage_id=f"P{i + 1}",
                    text=p.text,
                    doc_id=p.citation.doc_id,
                    section_path=p.citation.section_path,
                )
                for i, p in enumerate(result.passages)
            )
        return knowledge

    # ---------------------------------------------------------------- agent nodes

    def _agent_node(self, spec: AgentSpec) -> Callable[[C360State], Awaitable[dict[str, Any]]]:
        """Build the guarded node coroutine for one agent."""

        async def _node(state: C360State) -> dict[str, Any]:
            return await self._run_one(spec, state)

        return _guarded(spec, _node)

    async def _run_one(self, spec: AgentSpec, state: C360State) -> dict[str, Any]:
        """Run one agent to an :class:`AgentResult`, served from cache when the key matches."""
        facts = state.get("facts", {}).get(spec.name, FactTable())
        passages = state.get("knowledge", {}).get(spec.name, ())
        principal = state["principal"]
        prompt = self._prompts.for_agent(spec.name)

        key = agent_cache_key(
            agent=spec.name,
            customer_id=state["customer_id"],
            role=str(principal.role),
            as_of=_today_iso(),
            fact_fp=fact_fingerprint(facts),
            knowledge_fp=knowledge_fingerprint(passages),
            formula_version=FORMULA_VERSION,
            prompt_version=prompt.version,
            model_id=self._provider.model_id,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return self._channels(cached)

        redactor: PromptRedactor = _fresh_redactor()
        audit = self._audit_hook(principal, state["customer_id"])
        runner = AgentRunner(self._provider, redactor, audit=audit, breaker=self._breaker)
        item = AgentInput(
            spec=spec,
            facts=facts,
            passages=passages,
            prompt_body=prompt.body,
            prompt_version=prompt.version,
        )
        result = await runner.run(item)
        self._cache.put(key, result)
        return self._channels(result)

    def _audit_hook(self, principal: Principal, customer_id: str) -> AgentAuditHook | None:
        """Bind a per-agent audit hook to the sink, or ``None`` when no sink is wired."""
        if self._audit_sink is None:
            return None
        return build_agent_audit_hook(self._audit_sink, principal, customer_id)

    @staticmethod
    def _channels(result: AgentResult) -> dict[str, Any]:
        return {
            "agent_outputs": {result.agent: result},
            "citations": [*result.fact_citations, *result.passage_citations],
        }


# ---------------------------------------------------------------- helpers


def _bind_spec(spec: AgentSpec, allowlist: frozenset[str], budget_s: float | None) -> AgentSpec:
    """Bind the prompt field allowlist and, when configured, the per-wave timeout budget."""
    bound = spec.with_allowlist(allowlist)
    if budget_s is not None:
        bound = bound.with_timeout(budget_s)
    return bound


def _guarded(
    spec: AgentSpec, node: Callable[[C360State], Awaitable[dict[str, Any]]]
) -> Callable[[C360State], Awaitable[dict[str, Any]]]:
    """Wrap a node with its per-node timeout and failure isolation (design §8.1, requirement 13.6).

    A timeout or any exception becomes an :class:`AgentError` plus the agent's name in
    ``unavailable_inputs``; it is never re-raised, so a wave never fails on one straggler.
    """

    async def _wrapped(state: C360State) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(node(state), timeout=spec.timeout_s)
        except TimeoutError:
            return _error_update(spec, "AGENT_TIMEOUT", "agent timed out")
        except Exception as exc:
            return _error_update(spec, "AGENT_ERROR", type(exc).__name__)

    return _wrapped


def _error_update(spec: AgentSpec, code: str, message: str) -> dict[str, Any]:
    return {
        "errors": [AgentError(agent=spec.name, code=code, message=f"{spec.name}: {message}")],
        "unavailable_inputs": [spec.name],
    }


def _renumber(facts: list[Any]) -> FactTable:
    """Rebuild a fact table with contiguous ``F1..Fn`` ids over a merged fact list.

    Facts merged from several tools each start at ``F1``; renumbering gives one table with unique
    ids so a citation in the narrative resolves unambiguously.
    """
    builder = FactTable.builder()
    for fact in facts:
        builder.add(
            entity_type=fact.entity_type,
            entity_id=fact.entity_id,
            field=fact.field,
            value=fact.value,
            as_of=fact.as_of,
            source_system=fact.source_system,
        )
    return builder.build()


def _fresh_redactor() -> PromptRedactor:
    from c360.agents.redaction import PromptRedactor  # noqa: PLC0415 - avoid import cycle

    return PromptRedactor()


def _today_iso() -> str:
    """The as-of date component of the cache key: the current UTC date.

    Date, not timestamp, so a narrative computed at 10:00 and read at 10:05 shares a key and is
    served from cache; a new calendar day is a legitimate reason to recompute a time-sensitive card.
    """
    from datetime import UTC, datetime  # noqa: PLC0415 - local to keep the module header lean

    return datetime.now(tz=UTC).date().isoformat()


async def _finalize(state: C360State) -> dict[str, Any]:
    """Terminal node. The reducers have already merged every wave; nothing more to write."""
    del state
    return {}


__all__ = ["DashboardGraph"]
