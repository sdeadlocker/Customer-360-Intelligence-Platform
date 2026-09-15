"""Phase 8 — the agentic layer: LangGraph dashboard graph, agents and the Bedrock/mock providers.

Everything a model touches on the dashboard path lives here, and it is built so the *entire* suite
runs with no AWS account and no token spend: the provider seam (:mod:`c360.agents.provider`) selects
a :class:`~c360.agents.provider.MockLLMProvider` under ``LLM_PROVIDER=mock``, which renders
deterministic narratives from the same fact tables the live model would see (design §8.5). The mock
is not a stub — it is the CI/eval path *and* the circuit-breaker fallback, so a Bedrock outage
degrades an AI card to an accurate non-generated summary rather than a blank one.

The layers, in dependency order:

* :mod:`c360.agents.provider` — the ``LLMProvider`` port, the request/response value types, and the
  mock provider. The Bedrock implementation is :mod:`c360.agents.bedrock`, imported lazily so the
  AWS SDK is never a hard dependency (task 8.2).
* :mod:`c360.agents.prompts` — the versioned prompt registry (task 8.3): each prompt carries a
  content hash, the agent it binds to, its field allowlist and its knowledge domains, and its
  version flows onto every result and into the cache key.
* :mod:`c360.agents.redaction` — the :class:`PromptRedactor` (task 8.4): per-agent field allowlists,
  identifier pseudonymisation and re-hydration, applied to generation prompts and retrieval queries.
* :mod:`c360.agents.validator` — the claim validator (task 8.5): the gate that rejects a numeric
  claim not backed by a fact within tolerance, including a figure sourced only from a passage.
"""
