"""Task 8.10 — instrumentation, audit and labelling of every agent run (design §13.1).

Runs one agent through :class:`AgentRunner` and asserts: an ``invoke_agent`` span and a nested
``chat`` span are exported (through the real allowlist processor, so their attributes are provably
allowlisted), token usage rides on the chat span, no prompt/completion content leaks, the run is
audited with the model id and field manifest (never values), and the result carries the UI
labelling fields.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from c360.agents.prompts import parse_prompt
from c360.agents.provider import MockLLMProvider
from c360.agents.redaction import PromptRedactor
from c360.agents.runner import AgentInput, AgentRunner
from c360.agents.specs import AGENT_SPECS
from c360.tools.facts import FactTable

_PROMPT = """---
id: financial_health
version: v1
owner: platform
agent: financial_health
field_allowlist: net_worth_cents, customer_segment
knowledge_domains:
---
You are the financial-health agent. Cite each figure by its [F] id.
"""


def _item() -> AgentInput:
    prompt = parse_prompt(_PROMPT, source="financial_health.md")
    spec = AGENT_SPECS["financial_health"].with_allowlist(prompt.field_allowlist)
    facts = FactTable.builder()
    facts.add(entity_type="financial_profile", entity_id="C1", field="net_worth_cents", value=100)
    facts.add(entity_type="customer", entity_id="C1", field="customer_segment", value="HNW")
    return AgentInput(
        spec=spec,
        facts=facts.build(),
        passages=(),
        prompt_body=prompt.body,
        prompt_version=prompt.version,
    )


def _find(exporter: InMemorySpanExporter, name: str) -> Any:
    for span in exporter.get_finished_spans():
        if span.name == name:
            return span
    return None


def test_invoke_agent_and_chat_spans_are_exported(span_exporter: InMemorySpanExporter):
    runner = AgentRunner(MockLLMProvider(), PromptRedactor())
    asyncio.run(runner.run(_item()))

    invoke = _find(span_exporter, "invoke_agent financial_health")
    assert invoke is not None
    assert invoke.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert invoke.attributes["gen_ai.agent.name"] == "financial_health"
    assert invoke.attributes["c360.agent"] == "financial_health"
    assert invoke.attributes["c360.agent.wave"] == 1
    assert "+" in invoke.attributes["c360.prompt_version"]
    assert invoke.attributes["c360.degraded"] is False

    chat = _find(span_exporter, "chat financial_health")
    assert chat is not None
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.request.model"] == "mock-llm-v1"
    assert chat.attributes["gen_ai.response.model"] == "mock-llm-v1"


def test_chat_span_carries_token_usage(span_exporter: InMemorySpanExporter):
    runner = AgentRunner(MockLLMProvider(), PromptRedactor())
    asyncio.run(runner.run(_item()))
    chat = _find(span_exporter, "chat financial_health")
    assert chat.attributes["gen_ai.usage.input_tokens"] >= 1
    assert chat.attributes["gen_ai.usage.output_tokens"] >= 1


def test_no_prompt_or_completion_content_on_spans(span_exporter: InMemorySpanExporter):
    runner = AgentRunner(MockLLMProvider(), PromptRedactor())
    asyncio.run(runner.run(_item()))
    for span in span_exporter.get_finished_spans():
        for key in span.attributes or {}:
            # The allowlist drops prompt/completion content by default; assert none slipped through.
            assert "prompt" not in key or key == "c360.prompt_version"
            assert "completion" not in key
            assert "messages" not in key


def test_agent_run_is_audited_with_manifest_not_values():
    records: list[dict] = []

    def _hook(**kwargs: object) -> None:
        records.append(kwargs)

    runner = AgentRunner(MockLLMProvider(), PromptRedactor(), audit=_hook)
    asyncio.run(runner.run(_item()))

    assert len(records) == 1
    record = records[0]
    assert record["agent"] == "financial_health"
    assert record["model_id"] == "mock-llm-v1"
    assert "+" in record["prompt_version"]
    # The manifest lists field NAMES that survived; the value 100 is never in it.
    assert set(record["field_manifest"]) == {"net_worth_cents", "customer_segment"}
    assert "100" not in str(record["field_manifest"])
    assert record["degraded"] is False


def test_result_carries_labelling_metadata():
    runner = AgentRunner(MockLLMProvider(), PromptRedactor())
    result = asyncio.run(runner.run(_item()))
    assert result.model_id == "mock-llm-v1"
    assert "+" in result.prompt_version
    assert result.generated_at is not None
    assert result.degraded is False
