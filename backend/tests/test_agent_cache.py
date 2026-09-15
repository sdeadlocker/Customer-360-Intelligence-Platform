"""Task 8.8 — agent output caching (design §8.7).

Asserts the key folds in every component that could change the output (so a change misses the
cache), the TTL and ``cache_hit`` behaviour, ``invalidate_all`` for the recompute path, and — end to
end — that a second graph run over a shared cache serves every card from cache.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from c360.agents.cache import (
    AgentCache,
    agent_cache_key,
    fact_fingerprint,
    knowledge_fingerprint,
)
from c360.agents.graph import DashboardGraph
from c360.agents.prompts import build_prompt_registry
from c360.agents.provider import MockLLMProvider, PassageRef
from c360.agents.result import AgentResult
from c360.agents.schemas import FinancialHealthOutput
from c360.core.config import Settings
from c360.security.model import Role
from c360.tools import build_tool_registry
from c360.tools.facts import FactTable
from tests.phase5_fixtures import all_customer_ids, principal_for
from tests.test_agent_graph import _services  # reuse the service builder


def _key(**overrides: str) -> str:
    base = {
        "agent": "risk",
        "customer_id": "C1",
        "role": "RM",
        "as_of": "2026-09-13",
        "fact_fp": "ff",
        "knowledge_fp": "kf",
        "formula_version": "fhs-v1",
        "prompt_version": "v1+abc",
        "model_id": "mock-llm-v1",
    }
    base.update(overrides)
    return agent_cache_key(**base)


def _result(cache_hit: bool = False) -> AgentResult:
    return AgentResult(
        agent="risk",
        outputs=FinancialHealthOutput(narrative="ok"),
        generated_at=datetime.now(tz=UTC),
        model_id="mock-llm-v1",
        prompt_version="v1+abc",
        cache_hit=cache_hit,
    )


@pytest.mark.parametrize(
    "field",
    [
        "agent",
        "customer_id",
        "role",
        "as_of",
        "fact_fp",
        "knowledge_fp",
        "formula_version",
        "prompt_version",
        "model_id",
    ],
)
def test_every_key_component_changes_the_key(field):
    assert _key() != _key(**{field: "DIFFERENT"})


def test_identical_inputs_produce_identical_key():
    assert _key() == _key()


def test_fact_fingerprint_changes_with_a_value():
    a = FactTable.builder()
    a.add(entity_type="c", entity_id="1", field="x", value=100)
    b = FactTable.builder()
    b.add(entity_type="c", entity_id="1", field="x", value=101)
    assert fact_fingerprint(a.build()) != fact_fingerprint(b.build())


def test_knowledge_fingerprint_distinguishes_empty_from_nonempty():
    empty = knowledge_fingerprint(())
    one = knowledge_fingerprint(
        (PassageRef(passage_id="P1", text="t", doc_id="d", section_path="s"),)
    )
    assert empty != one


def test_put_then_get_marks_cache_hit():
    cache = AgentCache(ttl_s=900)
    cache.put("k", _result())
    got = cache.get("k")
    assert got is not None
    assert got.cache_hit is True


def test_miss_returns_none():
    assert AgentCache(ttl_s=900).get("absent") is None


def test_zero_ttl_disables_cache():
    cache = AgentCache(ttl_s=0)
    cache.put("k", _result())
    assert cache.get("k") is None


def test_expired_entry_is_a_miss(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("c360.agents.cache.time.monotonic", lambda: clock["now"])
    cache = AgentCache(ttl_s=10)
    cache.put("k", _result())
    assert cache.get("k") is not None  # within TTL
    clock["now"] += 11  # advance past the TTL
    assert cache.get("k") is None


def test_invalidate_all_clears_and_counts():
    cache = AgentCache(ttl_s=900)
    cache.put("a", _result())
    cache.put("b", _result())
    assert cache.invalidate_all() == 2
    assert cache.get("a") is None


# ---------------------------------------------------------------- end to end
def test_second_graph_run_serves_every_card_from_cache(phase5_engine: Engine, settings: Settings):
    cache = AgentCache(ttl_s=900)
    graph = DashboardGraph(
        registry=build_tool_registry(),
        services=_services(phase5_engine, settings),
        provider=MockLLMProvider(),
        prompts=build_prompt_registry(settings),
        cache=cache,
    )
    customer_id = all_customer_ids(phase5_engine)[0]
    principal = principal_for(Role.RM)

    first = asyncio.run(graph.arun(principal, customer_id))
    assert all(not r.cache_hit for r in first["agent_outputs"].values())

    second = asyncio.run(graph.arun(principal, customer_id))
    assert all(r.cache_hit for r in second["agent_outputs"].values())
    # The cached narratives are identical to the first run's.
    for name, result in second["agent_outputs"].items():
        cached = result.outputs.model_dump()["narrative"]
        original = first["agent_outputs"][name].outputs.model_dump()["narrative"]
        assert cached == original
