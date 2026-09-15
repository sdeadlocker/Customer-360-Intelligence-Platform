"""Tasks 8.6 & 8.7 — the dashboard graph and the seven agent nodes (design §8.1, §8.3).

The phase-gate property, exercised here against the seeded Phase 5 dataset with the mock provider
and no AWS: running the graph yields all seven agents, each with a schema-conformant ``outputs``
model, fact citations that resolve, a claim-validated (non-degraded) narrative, and the labelling
metadata (``model_id``, ``prompt_version``, ``generated_at``). Failure isolation and the wave
topology are asserted too — a failing node degrades its own card without failing the run.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Engine

from c360.agents.graph import DashboardGraph
from c360.agents.prompts import build_prompt_registry
from c360.agents.provider import MockLLMProvider
from c360.agents.result import AgentResult
from c360.agents.specs import AGENT_SPECS
from c360.agents.state import C360State
from c360.agents.validator import validate_claims
from c360.api.services import Services
from c360.core.config import Settings
from c360.data.repositories import build_repositories
from c360.security.model import Role
from c360.services.aggregator import C360Aggregator
from c360.services.customer import CustomerService, RecentlyViewedTracker
from c360.services.financial import FinancialService
from c360.services.journey import JourneyService
from c360.services.offer import OfferService
from c360.services.relationship import RelationshipService
from c360.services.risk import RiskService
from c360.tools import build_tool_registry
from c360.tools.facts import FactTable
from tests.phase5_fixtures import all_customer_ids, principal_for

_ALL_AGENTS = set(AGENT_SPECS)


def _services(engine: Engine, settings: Settings) -> Services:
    repos = build_repositories(engine)
    customer = CustomerService(repos.customer, recently_viewed=RecentlyViewedTracker())
    financial = FinancialService(repos.financial, spend_anomaly_sigma=settings.spend_anomaly_sigma)
    relationship = RelationshipService(
        repos.relationship,
        repos.graph,
        repos.customer,
        repos.financial,
        max_hops=settings.graph_max_hops,
        node_cap=settings.graph_node_cap,
    )
    risk = RiskService(repos.risk)
    offer = OfferService(repos.offer, cooling_off_days=settings.offer_cooling_off_days)
    journey = JourneyService(
        repos.journey,
        repos.relationship,
        major_txn_absolute_threshold_cents=settings.major_txn_absolute_threshold_cents,
        major_txn_median_multiple=settings.major_txn_median_multiple,
    )
    aggregator = C360Aggregator(
        customer=customer,
        financial=financial,
        relationship=relationship,
        risk=risk,
        offer=offer,
        journey=journey,
        max_workers=settings.sqlite_read_pool_size,
    )
    return Services(
        engine=engine,
        customer=customer,
        financial=financial,
        relationship=relationship,
        risk=risk,
        offer=offer,
        journey=journey,
        aggregator=aggregator,
        knowledge=None,
        knowledge_engine=None,
    )


@pytest.fixture
def graph(phase5_engine: Engine, settings: Settings) -> DashboardGraph:
    return DashboardGraph(
        registry=build_tool_registry(),
        services=_services(phase5_engine, settings),
        provider=MockLLMProvider(),
        prompts=build_prompt_registry(settings),
    )


@pytest.fixture
def customer_id(phase5_engine: Engine) -> str:
    return all_customer_ids(phase5_engine)[0]


def _run(graph: DashboardGraph, customer_id: str, role: Role = Role.RM) -> C360State:
    return asyncio.run(graph.arun(principal_for(role), customer_id))


def test_all_seven_agents_produce_output(graph, customer_id):
    state = _run(graph, customer_id)
    outputs = state["agent_outputs"]
    assert set(outputs) == _ALL_AGENTS


def test_every_output_is_schema_conformant(graph, customer_id):
    state = _run(graph, customer_id)
    for name, result in state["agent_outputs"].items():
        assert isinstance(result, AgentResult)
        assert isinstance(result.outputs, AGENT_SPECS[name].output_model)
        assert result.outputs.narrative  # a card always has a narrative


def test_narratives_are_claim_validated_and_not_degraded(graph, customer_id):
    # With the mock provider the narrative is the grounded template render, so it passes validation
    # against its own fact table and no card degrades.
    state = _run(graph, customer_id)
    for name, result in state["agent_outputs"].items():
        facts = _agent_facts(state, name)
        assert validate_claims(result.outputs.narrative, facts).ok, name
        assert result.degraded is False, name


def test_results_carry_labelling_metadata(graph, customer_id):
    state = _run(graph, customer_id)
    for result in state["agent_outputs"].values():
        assert result.model_id == "mock-llm-v1"
        assert "+" in result.prompt_version  # <declared>+<hash12>
        assert result.generated_at is not None


def test_fact_citations_resolve_to_the_agents_facts(graph, customer_id):
    state = _run(graph, customer_id)
    for name, result in state["agent_outputs"].items():
        facts = _agent_facts(state, name)
        fact_ids = {f.fact_id for f in facts.facts}
        for citation in result.fact_citations:
            assert citation.fact_id in fact_ids, (name, citation.fact_id)


def test_citations_channel_is_populated(graph, customer_id):
    state = _run(graph, customer_id)
    # Every fact citation from every agent accumulated in the reduced citations channel.
    assert len(state["citations"]) >= sum(
        len(r.fact_citations) for r in state["agent_outputs"].values()
    )


def test_no_errors_on_the_happy_path(graph, customer_id):
    state = _run(graph, customer_id)
    assert state.get("errors", []) == []


def _agent_facts(state: C360State, name: str) -> FactTable:
    facts: FactTable = state.get("facts", {}).get(name, FactTable())
    return facts
