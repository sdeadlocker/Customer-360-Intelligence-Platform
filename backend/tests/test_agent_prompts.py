"""Task 8.3 — the versioned prompt registry (design §14.7).

Asserts the properties the cache key and evaluation depend on: every wired agent has a prompt, the
effective version is bound to file content (so a body edit yields a new version), field allowlists
and knowledge domains parse as declared, and malformed files fail loudly.
"""

from __future__ import annotations

import pytest

from c360.agents.prompts import (
    PromptError,
    PromptRegistry,
    build_prompt_registry,
    parse_prompt,
)
from tests.conftest import make_settings

_SEVEN_AGENTS = {
    "financial_health",
    "risk",
    "life_event",
    "relationship",
    "offer_recommendation",
    "journey",
    "customer_summary",
}

# Phase 9 adds the natural-language Q&A prompt alongside the seven dashboard-agent prompts.
_ALL_AGENTS = _SEVEN_AGENTS | {"qa"}

_VALID = """---
id: demo
version: v3
owner: platform
agent: demo
field_allowlist: a, b, c
knowledge_domains: procedure, playbook
---
Body text here.
"""


def test_registry_loads_all_seven_prompt_files():
    registry = build_prompt_registry(make_settings())
    # The seven dashboard agents each have a prompt, plus the Phase 9 Q&A prompt.
    assert set(registry.agents()) == _ALL_AGENTS
    assert set(registry.agents()) >= _SEVEN_AGENTS


def test_only_retrieving_agents_declare_knowledge_domains():
    registry = build_prompt_registry(make_settings())
    assert registry.for_agent("risk").knowledge_domains == ("procedure",)
    assert registry.for_agent("life_event").knowledge_domains == ("playbook",)
    assert set(registry.for_agent("offer_recommendation").knowledge_domains) == {
        "product_catalog",
        "offer_terms",
    }
    # The four non-retrieving agents declare no domains.
    for agent in ("financial_health", "relationship", "journey", "customer_summary"):
        assert registry.for_agent(agent).knowledge_domains == ()


def test_version_is_bound_to_content_hash():
    prompt = parse_prompt(_VALID, source="demo.md")
    assert prompt.declared_version == "v3"
    assert prompt.version.startswith("v3+")
    assert prompt.content_hash

    # A one-character body change yields a different effective version and hash.
    changed = parse_prompt(_VALID.replace("Body text here.", "Body text here!"), source="demo.md")
    assert changed.declared_version == "v3"
    assert changed.version != prompt.version


def test_field_allowlist_and_body_parse():
    prompt = parse_prompt(_VALID, source="demo.md")
    assert prompt.field_allowlist == frozenset({"a", "b", "c"})
    assert prompt.knowledge_domains == ("procedure", "playbook")
    assert prompt.body == "Body text here."
    assert prompt.owner == "platform"


def test_missing_front_matter_fence_raises():
    with pytest.raises(PromptError, match="front-matter fence"):
        parse_prompt("no fence here\nbody", source="bad.md")


def test_unclosed_fence_raises():
    with pytest.raises(PromptError, match="not closed"):
        parse_prompt("---\nid: x\nagent: x\nversion: v1\nbody with no close", source="bad.md")


def test_missing_required_key_raises():
    with pytest.raises(PromptError, match="missing required key"):
        parse_prompt("---\nid: x\nagent: x\n---\nbody", source="bad.md")


def test_duplicate_agent_binding_raises():
    a = parse_prompt(_VALID, source="a.md")
    b = parse_prompt(_VALID, source="b.md")
    with pytest.raises(PromptError, match="bind to agent"):
        PromptRegistry([a, b])


def test_missing_agent_lookup_raises():
    registry = PromptRegistry([parse_prompt(_VALID, source="a.md")])
    with pytest.raises(PromptError, match="no prompt registered"):
        registry.for_agent("nope")
    assert registry.get("nope") is None
