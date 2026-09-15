"""Phase 9 — the Q&A ReAct graph, memory, grounding and citations (design §10.1, §10.2).

These run the real Q&A graph over the seeded Phase 5 dataset with the mock provider (no AWS), so the
assertions are about the loop's behaviour: the router classifies, the agent/tool loop grounds an
answer in returned facts with resolvable ``[F]`` citations, the tool-call budget is capped, a
non-entitled tool access is refused disclosing nothing, and conversation memory keyed by
``(session_id, customer_id)`` never leaks across customers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from c360.agents.breaker import BreakerState, CircuitBreaker
from c360.agents.prompts import build_prompt_registry
from c360.agents.provider import MockLLMProvider
from c360.agents.qa_graph import REFUSAL_TEXT, QaGraph
from c360.agents.qa_router import classify_route
from c360.agents.qa_runtime import QaMemory, thread_id
from c360.agents.qa_types import QaRoute
from c360.api.services import Services, build_services
from c360.core.config import Settings
from c360.security.entitlement import BookScope
from c360.security.model import Principal, Role, knowledge_levels_for_role
from c360.security.policy import policy_for_role
from c360.tools.registry import ToolRegistry, build_tool_registry
from tests.conftest import make_settings
from tests.phase5_fixtures import all_customer_ids, principal_for


@pytest.fixture(scope="module")
def qa_settings(phase5_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    work = tmp_path_factory.mktemp("qa_graph")
    return make_settings(
        sqlite_db_path=str(phase5_db),
        sqlite_audit_db_path=str(work / "audit.db"),
        sqlite_checkpoint_db_path=str(work / "checkpoints.db"),
        # Point knowledge at a path that does not exist so these graph tests run with no knowledge
        # base (knowledge=None) — the "no supporting guidance found" path — independent of any
        # data/knowledge.db that happens to be in the repo. The API suite ingests its own corpus.
        sqlite_knowledge_db_path=str(work / "no_knowledge.db"),
    )


@pytest.fixture(scope="module")
def services(qa_settings: Settings) -> Iterator[Services]:
    container = build_services(qa_settings)
    try:
        yield container
    finally:
        container.engine.dispose()


@pytest.fixture
def graph(qa_settings: Settings, services: Services) -> QaGraph:
    return QaGraph(
        registry=_registry(),
        services=services,
        provider=MockLLMProvider(),
        prompts=build_prompt_registry(qa_settings),
        max_tool_calls=qa_settings.qa_max_tool_calls,
    )


def _registry() -> ToolRegistry:
    return build_tool_registry()


def _rm(services: Services) -> Principal:
    return Principal(
        user_id="rm.tester",
        role=Role.RM,
        entitlement=principal_for(Role.RISK).entitlement,  # ALL scope
        field_policy=policy_for_role(Role.RM),
        knowledge_levels=knowledge_levels_for_role(Role.RM),
    )


def _customer(services: Services) -> str:
    return all_customer_ids(services.engine)[0]


# ---------------------------------------------------------------- router (task 9.1)


class TestRouter:
    def test_customer_figure_is_a_facts_question(self) -> None:
        assert classify_route("what is their net worth?") is QaRoute.FACTS

    def test_policy_rule_is_a_knowledge_question(self) -> None:
        assert classify_route("what are the HELOC eligibility rules?") is QaRoute.KNOWLEDGE

    def test_mixed_question_is_both(self) -> None:
        assert classify_route("is this customer eligible for the platinum card?") is QaRoute.BOTH


# ---------------------------------------------------------------- grounding (tasks 9.3, 9.4)


class TestGrounding:
    def test_answer_is_grounded_with_resolvable_fact_citations(self, graph: QaGraph, services):
        cid = _customer(services)
        state = asyncio.run(graph.arun(_rm(services), cid, "what is their net worth?"))

        assert state["route"] == QaRoute.FACTS.value
        assert state["fact_citations"], "a grounded answer must carry fact citations"
        # Every [F] id the answer cites resolves to a returned fact citation.
        cited_ids = {c["fact_id"] for c in state["fact_citations"]}
        for token in state["answer"].split():
            if token.startswith("[F") and token.endswith("]"):
                assert token.strip("[].,") in cited_ids
        assert state["degraded"] is False
        assert state["refused"] is False

    def test_fact_and_passage_citations_are_separate(self, graph: QaGraph, services):
        cid = _customer(services)
        state = asyncio.run(graph.arun(_rm(services), cid, "what is their net worth?"))
        # Two distinct lists, not one merged list (design §9.1).
        assert "fact_citations" in state
        assert "passage_citations" in state
        assert isinstance(state["fact_citations"], list)
        assert isinstance(state["passage_citations"], list)

    def test_no_guidance_when_knowledge_unavailable(self, graph: QaGraph, services):
        # phase5 services carry no knowledge.db, so a pure policy question finds no guidance.
        cid = _customer(services)
        state = asyncio.run(graph.arun(_rm(services), cid, "what is the wire transfer policy?"))
        assert state["no_guidance"] is True
        assert "no supporting guidance" in state["answer"].lower()


# ---------------------------------------------------------------- tool-call cap (task 9.1)


class TestToolCallCap:
    def test_tool_calls_never_exceed_the_configured_cap(self, graph: QaGraph, services):
        cid = _customer(services)
        state = asyncio.run(graph.arun(_rm(services), cid, "what is their net worth?"))
        assert 0 <= state.get("tool_calls_made", 0) <= 6


# ---------------------------------------------------------------- refusal (task 9.3, req 11.6)


class TestRefusal:
    def test_non_entitled_tool_access_is_refused_disclosing_nothing(self, graph: QaGraph, services):
        ids = all_customer_ids(services.engine)
        target = ids[0]
        # A book scope that excludes the target: the tool's authorize() raises EntitlementError
        # and the loop refuses. (A real route 404s at the gate; this proves the in-loop net.)
        restricted = Principal(
            user_id="rm.restricted",
            role=Role.RM,
            entitlement=BookScope(frozenset(ids[1:5])),
            field_policy=policy_for_role(Role.RM),
            knowledge_levels=knowledge_levels_for_role(Role.RM),
        )
        state = asyncio.run(graph.arun(restricted, target, "what is their net worth?"))
        assert state["refused"] is True
        assert state["answer"] == REFUSAL_TEXT
        assert state["fact_citations"] == []
        # The refusal discloses no figure about the customer.
        assert "cents" not in state["answer"].lower()


# ---------------------------------------------------------------- memory (task 9.2, req 11.8/11.9)


class TestConversationMemory:
    def test_followup_resumes_thread_context(self, qa_settings: Settings, services: Services):
        async def run() -> int:
            mem = QaMemory(qa_settings.checkpoint_db_path)
            cp = await mem.checkpointer()
            graph = QaGraph(
                registry=_registry(),
                services=services,
                provider=MockLLMProvider(),
                prompts=build_prompt_registry(qa_settings),
                max_tool_calls=6,
                checkpointer=cp,
            )
            cid = _customer(services)
            tid = thread_id("sess-1", cid)
            await graph.arun(_rm(services), cid, "what is their net worth?", thread_id=tid)
            second = await graph.arun(_rm(services), cid, "and their deposits?", thread_id=tid)
            count = len(second["messages"])
            await mem.aclose()
            return count

        # The second turn's history includes the first turn (user+assistant+tool messages), so the
        # message count is well above a single turn's — proof the thread carried context.
        assert asyncio.run(run()) > 3

    def test_customer_switch_deletes_prior_thread(self, qa_settings: Settings, services: Services):
        async def run() -> object:
            mem = QaMemory(qa_settings.checkpoint_db_path)
            cp = await mem.checkpointer()
            graph = QaGraph(
                registry=_registry(),
                services=services,
                provider=MockLLMProvider(),
                prompts=build_prompt_registry(qa_settings),
                max_tool_calls=6,
                checkpointer=cp,
            )
            ids = all_customer_ids(services.engine)
            c1, c2 = ids[0], ids[1]
            await mem.switch_customer("sess-2", c1)
            await graph.arun(
                _rm(services), c1, "what is their net worth?", thread_id=thread_id("sess-2", c1)
            )
            # Switching to another customer must delete c1's thread server-side (req 11.9).
            await mem.switch_customer("sess-2", c2)
            state = await cp.aget({"configurable": {"thread_id": thread_id("sess-2", c1)}})
            await mem.aclose()
            return state

        assert asyncio.run(run()) is None, "the prior customer's thread must be deleted on switch"


# ---------------------------------------------------------------- graceful degradation (§13.7)


class _FailingConverseProvider(MockLLMProvider):
    """A provider whose ``converse`` always fails, to simulate a down/throttled Bedrock.

    Subclasses the mock so ``complete``/``stream``/``embed`` still work; only the Q&A turn call is
    made to raise. Counts how many times the live turn was attempted so a test can assert an open
    breaker skips the provider entirely.
    """

    model_id = "failing-bedrock"

    def __init__(self) -> None:
        super().__init__()
        self.converse_calls = 0

    async def converse(self, request: object) -> object:  # type: ignore[override]
        self.converse_calls += 1
        raise RuntimeError("bedrock down")


def _graph_with(provider, breaker, qa_settings: Settings, services: Services) -> QaGraph:
    return QaGraph(
        registry=_registry(),
        services=services,
        provider=provider,
        prompts=build_prompt_registry(qa_settings),
        max_tool_calls=qa_settings.qa_max_tool_calls,
        breaker=breaker,
    )


class TestGracefulDegradation:
    """When Bedrock is unavailable the Q&A path still answers — grounded, flagged, never a 500."""

    def test_provider_failure_still_answers_grounded_and_flags_degraded(
        self, qa_settings: Settings, services: Services
    ) -> None:
        breaker = CircuitBreaker("generation", threshold=1, cooloff_s=100)
        graph = _graph_with(_FailingConverseProvider(), breaker, qa_settings, services)
        cid = _customer(services)

        state = asyncio.run(graph.arun(_rm(services), cid, "what is their net worth?"))

        # The request did not error; it produced a grounded, degraded answer via the mock fallback.
        assert state["degraded"] is True
        assert state["refused"] is False
        assert state["answer"].strip()

    def test_repeated_failure_trips_the_breaker(
        self, qa_settings: Settings, services: Services
    ) -> None:
        breaker = CircuitBreaker("generation", threshold=1, cooloff_s=100)
        graph = _graph_with(_FailingConverseProvider(), breaker, qa_settings, services)
        cid = _customer(services)

        asyncio.run(graph.arun(_rm(services), cid, "what is their net worth?"))

        assert breaker.state is BreakerState.OPEN

    def test_open_breaker_skips_the_provider(
        self, qa_settings: Settings, services: Services
    ) -> None:
        breaker = CircuitBreaker("generation", threshold=1, cooloff_s=100)
        breaker.record_failure()  # force open before the run
        provider = _FailingConverseProvider()
        graph = _graph_with(provider, breaker, qa_settings, services)
        cid = _customer(services)

        state = asyncio.run(graph.arun(_rm(services), cid, "what is their net worth?"))

        # Fail-fast: with the breaker open the live provider is never called, yet an answer is still
        # produced from the deterministic fallback.
        assert provider.converse_calls == 0
        assert state["degraded"] is True
        assert state["answer"].strip()

    def test_no_breaker_still_degrades_on_failure(
        self, qa_settings: Settings, services: Services
    ) -> None:
        # A graph built without a breaker (the existing default) must still fall back on failure —
        # the breaker only changes *when* the provider is skipped, not whether degradation happens.
        graph = _graph_with(_FailingConverseProvider(), None, qa_settings, services)
        cid = _customer(services)

        state = asyncio.run(graph.arun(_rm(services), cid, "what is their net worth?"))

        assert state["degraded"] is True
        assert state["answer"].strip()
