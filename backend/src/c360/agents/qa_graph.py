"""The natural-language Q&A ReAct graph (Phase 9, design §10.1).

A single LangGraph loop answers a question about one customer by reasoning over the *same* typed
tool registry the dashboard and the REST API use — never text-to-SQL, so the masking filter and the
entitlement gate a tool enforces are enforced here too (design §10.1). The topology is the ReAct
shape design §10.1 fixes::

    START -> router -> qa_agent -> (tool_calls?) -> tool_node -> qa_agent
                                 \\-> (no tool_calls) -> validate -> END

* **router** classifies the question as needing customer *facts*, institutional *knowledge* or
  *both* (task 9.1). The classification is advisory: a pure policy question skips customer tools and
  answers faster, but the agent is never *denied* a tool it asks for.
* **qa_agent** is one provider ``converse`` turn: the model either asks for tools or produces its
  final answer. The available tools are the whole registry's schemas.
* **tool_node** runs each requested tool through the registry — inside the same authorization,
  masking and audit path as REST — and appends the results to the conversation. A non-entitled
  customer id raises :class:`EntitlementError` from the tool; the loop turns that into a refusal
  that discloses nothing (requirement 11.6). ``knowledge_search`` returning nothing is the explicit
  "no supporting guidance found" signal (requirement 17.10).
* **validate** runs the claim validator (task 8.5) over the final answer: a figure not backed by a
  returned fact is rejected, the model is asked once to fix it, and a persistent violation degrades
  to a grounded, fact-listing fallback rather than shipping an ungrounded number.

The tool-call budget is capped at ``QA_MAX_TOOL_CALLS`` (default 6); LangGraph's ``recursion_limit``
is set from it so a runaway loop terminates deterministically (task 9.1).

Conversation memory (task 9.2) is layered on by :class:`~c360.agents.qa_runtime`, which compiles
this graph with an ``AsyncSqliteSaver`` checkpointer; this module is checkpointer-agnostic and
simply keeps the running ``messages`` on the state so a follow-up turn resumes with context.
"""

from __future__ import annotations

import operator
from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from c360.agents.qa_grounding import QaAccumulator
from c360.agents.qa_router import classify_route
from c360.agents.qa_types import (
    ConverseRequest,
    ConverseResponse,
    QaMessage,
    QaRoute,
    QaToolCall,
)
from c360.agents.qa_wire import current_turn as _current_turn
from c360.agents.qa_wire import encode_tool_result
from c360.agents.validator import validate_claims
from c360.core.telemetry import SpanAttr, get_tracer

# These names appear in ``QaState``'s annotations, which LangGraph resolves at runtime via
# ``get_type_hints``; they must be importable at module scope, not merely under TYPE_CHECKING, or
# graph construction raises ``NameError`` on the forward reference (mirrors c360.agents.state).
from c360.security.errors import EntitlementError
from c360.security.model import Principal
from c360.tools.facts import FactTable
from c360.tools.knowledge_tool import KnowledgeToolResult, KnowledgeUnavailableError
from c360.tools.registry import ToolContext
from c360.tools.schemas import tool_schemas

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.agents.breaker import CircuitBreaker
    from c360.agents.prompts import PromptRegistry
    from c360.agents.provider import LLMProvider
    from c360.api.services import Services
    from c360.tools.registry import ToolRegistry

_TRACER_NAME = "c360.agents.qa"

#: The message role marking the loop's final answer once validated.
_ASSISTANT = "assistant"

#: The refusal a non-entitled request receives, disclosing nothing about the customer (11.6).
REFUSAL_TEXT = "I can't share information about that customer."
#: The out-of-scope reply when the model produced neither a tool call nor grounded content.
NO_GUIDANCE_TEXT = "No supporting guidance was found for this question."


class QaState(TypedDict, total=False):
    """The Q&A loop's state (design §10.1).

    ``messages`` is the running conversation, reduced by append so each turn extends it; it is the
    channel the checkpointer persists for follow-up context (task 9.2). The remaining channels are
    per-turn outputs the loop fills as it resolves: the classified ``route``, the final ``answer``,
    the two citation lists kept apart, any graph ``traversal_paths``, and the behavioural flags the
    route surfaces to the client (``refused``, ``no_guidance``, ``degraded``).

    ``customer_id`` is the single customer a question is scoped to when the graph is entered from a
    customer's dashboard (``/customers/{id}/ask``). It is ``None`` on the cross-customer entry point
    (``/ask``, the search-landing "Ask anything" surface), where the agent must first call
    ``customer_search`` to resolve which customer a question is about before reading their details.
    Either way every reading tool re-authorizes the customer id it is handed, so entitlement is
    enforced per result, not by a single up-front gate.
    """

    customer_id: str | None
    principal: Principal
    question: str
    route: str
    messages: Annotated[list[QaMessage], operator.add]
    tool_calls_made: Annotated[int, operator.add]
    answer: str
    fact_citations: list[dict[str, Any]]
    passage_citations: list[dict[str, Any]]
    traversal_paths: list[list[str]]
    refused: bool
    no_guidance: bool
    degraded: bool


class QaGraph:
    """Builds and runs the Q&A ReAct graph for one principal + customer (task 9.1)."""

    __slots__ = (
        "_breaker",
        "_compiled",
        "_max_tool_calls",
        "_prompts",
        "_provider",
        "_registry",
        "_services",
        "_tool_schemas",
    )

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        services: Services,
        provider: LLMProvider,
        prompts: PromptRegistry,
        max_tool_calls: int,
        checkpointer: Any = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._registry = registry
        self._services = services
        self._provider = provider
        self._prompts = prompts
        self._max_tool_calls = max_tool_calls
        self._tool_schemas = tuple(tool_schemas(registry))
        # Optional generation circuit breaker (task 8.11, design §13.7), shared with the dashboard
        # graph. When open, `_qa_agent` skips the provider and answers from the deterministic mock
        # turn immediately, so a sustained Bedrock outage no longer costs a failing round-trip on
        # every turn — it fails fast to the grounded fallback until Bedrock recovers.
        self._breaker = breaker
        self._compiled = self._build().compile(checkpointer=checkpointer)

    @property
    def compiled(self) -> Any:
        """The compiled graph, exposed for the SSE path's ``astream_events`` (task 9.5)."""
        return self._compiled

    @property
    def recursion_limit(self) -> int:
        """LangGraph recursion budget derived from the tool-call cap (task 9.1).

        The loop is router -> (agent -> tools) x N -> agent -> validate. Each tool round is two
        supersteps (agent + tool_node); the cap is on *tool calls*, so the recursion limit is set
        generously above ``2 * max_tool_calls`` plus the fixed router/validate steps, and the hard
        cap on tool calls is enforced explicitly in the agent node regardless.
        """
        return 2 * self._max_tool_calls + 4

    async def arun(
        self,
        principal: Principal,
        customer_id: str | None,
        question: str,
        *,
        thread_id: str | None = None,
    ) -> QaState:
        """Answer ``question`` for ``principal``, scoped to ``customer_id`` when one is given.

        With a ``customer_id`` the run answers about that one customer (the dashboard path). With
        ``customer_id=None`` the run is the cross-customer search path: the agent resolves the
        customer itself via ``customer_search`` before reading any details, and every reading tool
        still re-authorizes the id it resolves to, so entitlement holds per result.

        When ``thread_id`` is given the run is checkpointed under it, so a follow-up on the same
        thread resumes with the prior conversation (task 9.2). Without one the run is stateless,
        which is what a one-shot test or an eval invocation wants.
        """
        result = await self._compiled.ainvoke(
            self._initial(principal, customer_id, question),
            config=self._config(thread_id),
        )
        return cast("QaState", result)

    def _initial(self, principal: Principal, customer_id: str | None, question: str) -> QaState:
        return {
            "principal": principal,
            "customer_id": customer_id,
            "question": question,
            "messages": [QaMessage(role="user", content=question)],
        }

    def _config(self, thread_id: str | None) -> RunnableConfig:
        config: RunnableConfig = {"recursion_limit": self.recursion_limit}
        if thread_id is not None:
            config["configurable"] = {"thread_id": thread_id}
        return config

    # ---------------------------------------------------------------- build

    def _build(self) -> StateGraph:
        graph: StateGraph = StateGraph(QaState)
        graph.add_node("router", self._router)
        graph.add_node("qa_agent", self._qa_agent)
        graph.add_node("tool_node", self._tool_node)
        graph.add_node("validate", self._validate)

        graph.add_edge(START, "router")
        graph.add_edge("router", "qa_agent")
        graph.add_conditional_edges(
            "qa_agent",
            self._route_after_agent,
            {"tools": "tool_node", "answer": "validate"},
        )
        graph.add_edge("tool_node", "qa_agent")
        graph.add_edge("validate", END)
        return graph

    # ---------------------------------------------------------------- router

    async def _router(self, state: QaState) -> dict[str, Any]:
        """Classify the question as facts / knowledge / both (task 9.1, design §10.1).

        Also zeroes the tool-call counter for this turn. The counter uses an additive reducer so it
        accumulates within one question; on a checkpointed thread it would otherwise carry over from
        the previous turn, so the router writes a compensating value that resets the running total
        to zero before this turn's agent reads it — the cap is per question, not per thread.
        """
        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span("qa_router") as span:
            route = classify_route(state["question"])
            span.set_attribute("gen_ai.operation.name", "qa_router")
            span.set_attribute("c360.qa.route", route.value)
            span.set_attribute(SpanAttr.OUTCOME, "ok")
        return {"route": route.value, "tool_calls_made": -state.get("tool_calls_made", 0)}

    # ---------------------------------------------------------------- qa_agent

    async def _qa_agent(self, state: QaState) -> dict[str, Any]:
        """One model turn: ask for tools, or produce the final answer (task 9.1).

        When the tool-call budget is already exhausted the tools are withheld from this turn so the
        model is forced to answer from what it has, which terminates the loop deterministically at
        the cap (requirement 11.2, the six-call ceiling).
        """
        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span("qa_agent") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "qa_agent")
            budget_left = self._max_tool_calls - state.get("tool_calls_made", 0)
            tools = self._tool_schemas if budget_left > 0 else ()
            request = ConverseRequest(
                system=self._system(state),
                messages=tuple(state.get("messages", [])),
                tools=tools,
                route=QaRoute(state.get("route", QaRoute.BOTH.value)),
            )
            # Resilience: if the live model provider fails (Bedrock down, throttled, timed out, or
            # credentials/access missing), fall back to the deterministic offline turn rather than
            # letting the whole /ask request error out. The fallback drives the same ReAct loop —
            # resolving the customer and reading their real facts through the same tools — so the
            # answer stays grounded in the customer's data with no LLM. The run is flagged
            # `degraded` so the UI badges it (design §13.7: the generation breaker's template
            # fallback, applied to the Q&A path).
            #
            # The generation breaker (shared with the dashboard graph) short-circuits this: once it
            # is open from repeated failures, the provider is skipped entirely and the turn answers
            # from the mock immediately, so a sustained outage does not pay a failing round-trip on
            # every turn. A half-open trial is allowed through and closes the breaker on success.
            degraded_turn = False
            if self._breaker is not None and not self._breaker.allow():
                response = self._degraded_turn(request)
                degraded_turn = True
                span.set_attribute("c360.qa.breaker_open", True)
                span.set_attribute("c360.qa.degraded", True)
                span.set_attribute(SpanAttr.OUTCOME, "degraded")
            else:
                try:
                    response = await self._provider.converse(request)
                except Exception:
                    if self._breaker is not None:
                        self._breaker.record_failure()
                    response = self._degraded_turn(request)
                    degraded_turn = True
                    span.set_attribute("c360.qa.degraded", True)
                    span.set_attribute(SpanAttr.OUTCOME, "degraded")
                else:
                    if self._breaker is not None:
                        self._breaker.record_success()
                    span.set_attribute(SpanAttr.OUTCOME, "ok")
            span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
            span.set_attribute("c360.qa.wants_tools", response.wants_tools)

        message = QaMessage(
            role=_ASSISTANT,
            content=response.text,
            tool_calls=response.tool_calls,
        )
        update: dict[str, Any] = {"messages": [message]}
        if degraded_turn:
            update["degraded"] = True
        return update

    def _degraded_turn(self, request: ConverseRequest) -> ConverseResponse:
        """The deterministic offline turn used when live generation is unavailable (§13.7).

        Drives the same ReAct loop through :func:`mock_converse`, so the fallback resolves the
        customer and reads real facts through the same tools — the answer stays grounded, just not
        model-authored. Isolated so the breaker-open and provider-failure branches share one path.
        """
        from c360.agents.qa_mock import mock_converse  # noqa: PLC0415 - avoid import cycle

        return mock_converse(request, model_id=self._provider.model_id)

    @staticmethod
    def _route_after_agent(state: QaState) -> str:
        """Conditional edge: loop to the tools if the last turn asked for any, else validate."""
        messages = state.get("messages", [])
        if messages and messages[-1].tool_calls:
            return "tools"
        return "answer"

    # ---------------------------------------------------------------- tool_node

    async def _tool_node(self, state: QaState) -> dict[str, Any]:
        """Run every tool the last agent turn requested, appending results to the conversation.

        Each tool runs through the registry — same authorization, masking and span as REST. A
        non-entitled customer id raises :class:`EntitlementError`; the loop stops and refuses,
        disclosing nothing (requirement 11.6). Any other tool failure becomes a tool message the
        model can react to, rather than crashing the turn.
        """
        principal = state["principal"]
        context = ToolContext(principal=principal, services=self._services)
        last = state.get("messages", [])[-1]
        accumulator = QaAccumulator()

        tool_messages: list[QaMessage] = []
        for call in last.tool_calls:
            try:
                message = self._run_one_tool(context, call, accumulator)
            except EntitlementError:
                # Refuse and disclose nothing — the audit of the denied access is written by the
                # route (which owns the audit sink), mirroring how the REST gate audits denial.
                return {
                    "answer": REFUSAL_TEXT,
                    "refused": True,
                    "messages": [QaMessage(role=_ASSISTANT, content=REFUSAL_TEXT)],
                    "tool_calls_made": len(last.tool_calls),
                }
            tool_messages.append(message)

        update: dict[str, Any] = {
            "messages": tool_messages,
            "tool_calls_made": len(last.tool_calls),
        }
        paths = accumulator.traversal_paths()
        if paths:
            update["traversal_paths"] = [list(path) for path in paths]
        return update

    def _run_one_tool(
        self, context: ToolContext, call: QaToolCall, accumulator: QaAccumulator
    ) -> QaMessage:
        """Execute one tool call and encode its result as a ``tool`` message (tasks 9.3, 9.4)."""
        if call.name == "knowledge_search":
            return self._run_knowledge_tool(context, call, accumulator)
        return self._run_customer_tool(context, call, accumulator)

    def _run_customer_tool(
        self, context: ToolContext, call: QaToolCall, accumulator: QaAccumulator
    ) -> QaMessage:
        result = self._registry.execute(call.name, context, call.arguments)
        facts = getattr(result, "facts", None)
        merged: FactTable | None = None
        if isinstance(facts, FactTable):
            # The merged fact table owns the ids the model must cite; renumber the encoded facts so
            # the model sees the run-global F-ids, not this call's local F1..Fn.
            remap = accumulator.add_facts(facts)
            merged = _remap_fact_table(facts, remap)
        data = _traversal_data(call, result, accumulator)
        if data is None and call.name == "customer_search":
            # The resolver's payload is not facts or a traversal path — it is the list of matched
            # customers the agent must read to pick a customer_id (task 9.6). Forward it as DATA so
            # the model (and the mock) can carry the resolved id into the reading tools.
            data = getattr(result, "data", None)
        content = encode_tool_result(facts=merged, data=data)
        return QaMessage(role="tool", content=content, tool_call_id=call.call_id, name=call.name)

    def _run_knowledge_tool(
        self, context: ToolContext, call: QaToolCall, accumulator: QaAccumulator
    ) -> QaMessage:
        args = {**call.arguments, "rerank": True}  # rerank enabled on the Q&A path (task 9.5)
        try:
            result = self._registry.execute(call.name, context, args)
        except KnowledgeUnavailableError:
            content = encode_tool_result(data={"no_guidance_found": True})
            return QaMessage(
                role="tool", content=content, tool_call_id=call.call_id, name=call.name
            )
        passages = _passage_dicts(result)
        numbered = accumulator.add_passages(passages)
        content = encode_tool_result(
            passages=numbered,
            data={"no_guidance_found": len(numbered) == 0},
        )
        return QaMessage(role="tool", content=content, tool_call_id=call.call_id, name=call.name)

    # ---------------------------------------------------------------- validate

    async def _validate(self, state: QaState) -> dict[str, Any]:
        """Validate the answer's figures against returned facts; assemble the final output (9.3)."""
        if state.get("refused"):
            return self._finalize_refusal()

        messages = _current_turn(state.get("messages", []))
        accumulator = _accumulator_from_messages(messages)
        answer = _last_assistant_text(messages)
        facts = accumulator.merged_facts()
        no_guidance = _knowledge_requested(messages) and not accumulator.has_passages

        # Carry any degraded flag a prior turn set (e.g. a provider fallback in `_qa_agent`), so a
        # deterministic-fallback answer stays badged even if validation itself is clean.
        degraded = bool(state.get("degraded", False))
        if no_guidance and not facts.facts:
            # Knowledge was consulted and returned nothing, and there are no customer facts to fall
            # back on: give the canonical "no supporting guidance" reply (requirement 17.10) rather
            # than whatever phrasing the model produced.
            answer = NO_GUIDANCE_TEXT
        elif answer.strip():
            verdict = validate_claims(answer, facts)
            if not verdict.ok:
                # A persistent ungrounded figure degrades to a deterministic, fact-listing answer
                # rather than shipping the number (task 8.5 discipline applied to Q&A).
                answer = _grounded_fallback(facts, accumulator)
                degraded = True
        else:
            answer = NO_GUIDANCE_TEXT if no_guidance else _grounded_fallback(facts, accumulator)

        return {
            "answer": answer,
            "fact_citations": [c.model_dump(mode="json") for c in accumulator.fact_citations()],
            "passage_citations": [
                c.model_dump(mode="json") for c in accumulator.passage_citations()
            ],
            "traversal_paths": [list(p) for p in accumulator.traversal_paths()],
            "no_guidance": no_guidance,
            "degraded": degraded,
            "refused": False,
        }

    @staticmethod
    def _finalize_refusal() -> dict[str, Any]:
        return {
            "answer": REFUSAL_TEXT,
            "fact_citations": [],
            "passage_citations": [],
            "traversal_paths": [],
            "no_guidance": False,
            "degraded": False,
            "refused": True,
        }

    # ---------------------------------------------------------------- system prompt

    def _system(self, state: QaState) -> str:
        """The Q&A system instruction, from the prompt registry, scoped to the entry point.

        On the single-customer (dashboard) path the ``customer_id=<id>`` marker lets a tool call
        default its ``customer_id`` argument and lets the mock provider target the right customer;
        it is not identifying PII (it is an opaque internal id, already the path parameter the
        caller supplied). On the cross-customer (search-landing) path there is no fixed customer,
        so the marker is dropped and a directive is appended telling the agent to resolve the
        customer with ``customer_search`` first, asking a clarifying question on an ambiguous match.
        """
        prompt = self._prompts.get("qa")
        body = prompt.body if prompt is not None else _DEFAULT_QA_SYSTEM
        route = state.get("route", "both")
        customer_id = state.get("customer_id")
        if customer_id is None:
            return f"{body}\n\n{_CROSS_CUSTOMER_DIRECTIVE} route={route}"
        return f"{body}\n\ncustomer_id={customer_id} route={route}"


# ---------------------------------------------------------------- module helpers


_DEFAULT_QA_SYSTEM = (
    "You answer questions about banking customers for a relationship manager. Use the provided "
    "tools to fetch customer facts and institutional knowledge; never invent a figure. State a "
    "number only if it appears in a returned [F] fact and cite it with that id. Cite guidance with "
    "its [P] id. If the question is ambiguous, ask one clarifying question. If it is out of scope, "
    "say so plainly. If no guidance is found, say no supporting guidance was found."
)

#: Appended to the system prompt on the cross-customer (search-landing) path, where no single
#: customer is fixed. It tells the agent to resolve the customer itself before reading any details,
#: and to ask rather than guess when a query is not specific enough to identify one person.
_CROSS_CUSTOMER_DIRECTIVE = (
    "No customer is preselected. First call customer_search to find the customer the question is "
    "about, then use their customer_id with the other tools. If the search returns several people "
    "and the question does not disambiguate, ask one clarifying question naming the candidates "
    "rather than guessing. If it returns none, say no matching customer was found. Only ever "
    "discuss customers returned by customer_search for this request."
)


def _remap_fact_table(facts: FactTable, remap: dict[str, str]) -> FactTable:
    """Return ``facts`` with each fact's id replaced by its run-global id from ``remap``."""
    if not remap:
        return facts
    return FactTable(
        facts=tuple(
            fact.model_copy(update={"fact_id": remap.get(fact.fact_id, fact.fact_id)})
            for fact in facts.facts
        )
    )


def _traversal_data(
    call: QaToolCall, result: Any, accumulator: QaAccumulator
) -> dict[str, Any] | None:
    """Pull a graph traversal path out of a graph tool's result, if this was a graph call (9.4)."""
    if call.name not in ("graph_path", "graph_neighborhood"):
        return None
    data = getattr(result, "data", None)
    if isinstance(data, dict) and isinstance(data.get("path"), list):
        path = [str(node) for node in data["path"]]
        accumulator.add_traversal_path(path)
        return {"path": path, "length": data.get("length", max(0, len(path) - 1))}
    return None


def _passage_dicts(result: Any) -> list[dict[str, Any]]:
    """The passage rows from a ``KnowledgeToolResult``, as plain dicts for the accumulator."""
    if not isinstance(result, KnowledgeToolResult):
        return []
    return [
        {
            "passage_id": f"P{i + 1}",
            "text": p.text,
            "doc_id": p.doc_id,
            "section_path": p.section_path,
            "title": p.title,
            "version": p.version,
            "effective_from": p.effective_from,
            "effective_to": p.effective_to,
        }
        for i, p in enumerate(result.passages)
    ]


def _accumulator_from_messages(messages: Sequence[QaMessage]) -> QaAccumulator:
    """Rebuild the run's accumulator from the encoded tool messages, for the final assembly.

    The accumulator is transient (not on the checkpointed state), so at validation time it is
    reconstructed from the ``tool`` messages' encoded blocks — the single source of truth both the
    model and the citations draw from.
    """
    from c360.agents.qa_wire import decode_facts, decode_passages  # noqa: PLC0415

    accumulator = QaAccumulator()
    builder = FactTable.builder()
    for row in decode_facts(messages):
        builder.add(
            entity_type=row.get("entity_type", ""),
            entity_id=row.get("entity_id", ""),
            field=row.get("field", ""),
            value=row.get("value"),
        )
    accumulator.add_facts(builder.build())
    accumulator.add_passages(decode_passages(messages))
    for message in messages:
        for path in _paths_in_message(message):
            accumulator.add_traversal_path(path)
    return accumulator


def _paths_in_message(message: QaMessage) -> list[list[str]]:
    """Extract any traversal path a tool message's DATA block carried."""
    import json  # noqa: PLC0415

    if message.role != "tool" or not message.content:
        return []
    paths: list[list[str]] = []
    for block in message.content.split("\n\n"):
        if block.startswith("DATA:\n"):
            try:
                data = json.loads(block[len("DATA:\n") :])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("path"), list):
                paths.append([str(node) for node in data["path"]])
    return paths


def _last_assistant_text(messages: Sequence[QaMessage]) -> str:
    for message in reversed(messages):
        if message.role == _ASSISTANT and message.content:
            return message.content
    return ""


def _knowledge_requested(messages: Sequence[QaMessage]) -> bool:
    return any(
        message.role == "tool" and message.name == "knowledge_search" for message in messages
    )


def _grounded_fallback(facts: FactTable, accumulator: QaAccumulator) -> str:
    """A deterministic answer listing the returned facts, each cited — never an invented figure."""
    if not facts.facts:
        return NO_GUIDANCE_TEXT
    lines = ["Based on the customer's record:"]
    for fact in facts.facts:
        lines.append(f"- {fact.field}: {fact.value} [{fact.fact_id}]")
    return "\n".join(lines)


__all__ = ["NO_GUIDANCE_TEXT", "REFUSAL_TEXT", "QaGraph", "QaState"]
