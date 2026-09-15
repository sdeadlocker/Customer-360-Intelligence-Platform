"""Run one agent end to end: redact -> generate -> validate -> retry -> fallback (tasks 8.5-8.7).

This is the single place the grounding contract is enforced for the dashboard agents, so the graph
node (task 8.6) and the SSE path (task 8.9) share exactly one implementation and cannot drift. The
sequence for one agent:

1. **Redact** the agent's fact table to its prompt's field allowlist and pseudonymise identifiers
   (task 8.4). Only the surviving facts are dispatched, and the manifest is recorded for audit.
2. **Generate** a narrative from the redacted facts and the (already fenced) passages via the
   provider (task 8.1/8.2).
3. **Validate** the narrative's numeric claims against the *redacted* fact table (task 8.5). The
   validator runs against exactly the facts the model was given, so a figure is grounded iff it was
   dispatchable.
4. On a violation, **retry once** with the violation named. If it still fails — or if generation
   raised, or the breaker is open — **fall back** to the deterministic template render and mark the
   result ``degraded`` (task 8.11 provides the breaker; the fallback render is task 8.1's template).
5. **Re-hydrate** pseudonyms in the accepted narrative and build the :class:`AgentResult` with fact
   and passage citations resolved from the (unredacted) fact table and the passages.

The runner is provider-agnostic and offline-safe: with the mock provider the narrative is the
template render, which passes validation by construction, so the happy path and the fallback path
converge on the same accurate text — which is why the whole suite runs with no AWS.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from c360.agents.provider import CompletionRequest, CompletionResponse, PassageRef
from c360.agents.result import AgentResult, FactCitation, PassageCitation
from c360.agents.templates import TemplateRenderer
from c360.agents.validator import validate_claims
from c360.core.telemetry import SpanAttr, get_metrics, get_tracer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.agents.audit import AgentAuditHook
    from c360.agents.breaker import CircuitBreaker
    from c360.agents.provider import LLMProvider
    from c360.agents.redaction import PromptRedactor
    from c360.agents.specs import AgentSpec
    from c360.tools.facts import FactTable

_TRACER_NAME = "c360.agents"


@dataclass(frozen=True, slots=True)
class AgentInput:
    """Everything the runner needs to run one agent: its spec, its facts and its passages."""

    spec: AgentSpec
    facts: FactTable
    passages: tuple[PassageRef, ...]
    prompt_body: str
    prompt_version: str
    #: Which declared inputs were unavailable when ``load_context`` built this (e.g. a failed tool).
    unavailable_inputs: tuple[str, ...] = ()


class AgentRunner:
    """Runs a single agent through the grounding pipeline (tasks 8.5-8.7, 8.10, 8.11)."""

    __slots__ = ("_audit", "_breaker", "_provider", "_redactor", "_renderer")

    def __init__(
        self,
        provider: LLMProvider,
        redactor: PromptRedactor,
        audit: AgentAuditHook | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._provider = provider
        self._redactor = redactor
        self._renderer = TemplateRenderer()
        self._audit = audit
        self._breaker = breaker

    async def run(self, item: AgentInput) -> AgentResult:
        """Produce a validated, cited, labelled :class:`AgentResult` for ``item`` (task 8.10).

        The ``invoke_agent`` span carries the agent name, wave and prompt version; each model call
        underneath is its own ``chat`` span with model id, token usage and duration, following the
        GenAI semantic conventions (design §13.1). The agent run is audited once with the model id,
        the prompt version and the field manifest — *what* was dispatched, never the values.
        """
        tracer = get_tracer(_TRACER_NAME)
        started = time.perf_counter()
        with tracer.start_as_current_span(f"invoke_agent {item.spec.name}") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", item.spec.name)
            span.set_attribute(SpanAttr.AGENT, item.spec.name)
            span.set_attribute(SpanAttr.AGENT_WAVE, item.spec.wave)
            span.set_attribute(SpanAttr.PROMPT_VERSION, item.prompt_version)

            redacted = self._redactor.redact(item.facts, allowlist=item.spec.field_allowlist)
            request = CompletionRequest(
                agent=item.spec.name,
                system=item.prompt_body,
                task=item.spec.task,
                facts=redacted.facts,
                passages=item.passages,
                max_tokens=item.spec.max_tokens,
            )
            text, model_id, degraded, usage = await self._generate_validated(
                request, redacted.facts, span
            )
            span.set_attribute(SpanAttr.DEGRADED, degraded)
            span.set_attribute(SpanAttr.OUTCOME, "ok")

            self._record(
                item, model_id=model_id, manifest=redacted.field_manifest, degraded=degraded
            )

            narrative = self._redactor.rehydrate(text)
            outputs = item.spec.build_outputs(narrative, item.facts)
            result = AgentResult(
                agent=item.spec.name,
                outputs=outputs,
                fact_citations=_fact_citations(item.facts),
                passage_citations=_passage_citations(item.passages),
                unavailable_inputs=item.unavailable_inputs,
                confidence=item.spec.confidence(item.facts, item.unavailable_inputs),
                generated_at=datetime.now(tz=UTC),
                model_id=model_id,
                prompt_version=item.prompt_version,
                cache_hit=False,
                degraded=degraded,
            )
            self._record_metrics(item, result, model_id=model_id, usage=usage, started=started)
            return result

    def _record_metrics(
        self,
        item: AgentInput,
        result: AgentResult,
        *,
        model_id: str,
        usage: TokenUsageTuple,
        started: float,
    ) -> None:
        """Emit the agent-outcome, citation, and model token/cost metrics (task 10.2).

        Outcome is ``degraded`` when the template fallback ran, ``success`` otherwise — the
        timeout and cache-hit outcomes are recorded by the graph node and the cache layer, which
        own those decisions. Token usage is zero on the breaker/fallback path, and the cost counter
        floors a zero-token call to zero, so a degraded card costs nothing on the spend dashboard.
        """
        metrics = get_metrics()
        if metrics is None:
            return
        duration_ms = (time.perf_counter() - started) * 1000
        citations = len(result.fact_citations) + len(result.passage_citations)
        metrics.record_agent(
            agent=item.spec.name,
            outcome="degraded" if result.degraded else "success",
            duration_ms=duration_ms,
            citations=citations,
        )
        input_tokens, output_tokens = usage
        if input_tokens or output_tokens:
            metrics.record_model_call(
                model=model_id,
                agent=item.spec.name,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    def _record(
        self,
        item: AgentInput,
        *,
        model_id: str,
        manifest: tuple[str, ...],
        degraded: bool,
    ) -> None:
        """Write the per-agent audit record, if an audit hook was provided (task 8.10, req 10.14).

        The hook is fed *what* was sent to the model — the agent, the model id, the prompt version
        and the surviving field manifest — never the field values, which is the audit contract
        (design §7.4). An audit failure must not break the card, so it is best-effort here; the
        route already fail-closed-audited the AI access before streaming began.
        """
        if self._audit is None:
            return
        self._audit(
            agent=item.spec.name,
            model_id=model_id,
            prompt_version=item.prompt_version,
            field_manifest=manifest,
            retrieved_doc_ids=tuple({p.doc_id for p in item.passages}),
            degraded=degraded,
        )

    async def _generate_validated(
        self, request: CompletionRequest, facts: FactTable, span: object
    ) -> tuple[str, str, bool, TokenUsageTuple]:
        """Generate, validate, retry once, then fall back to the template (task 8.5, 8.11).

        Returns ``(text, model_id, degraded, usage)``. ``degraded`` is true when the template
        fallback was used — because generation failed, or the retry still produced an ungrounded
        claim.
        """
        set_attr = getattr(span, "set_attribute", lambda *_: None)

        # Generation circuit breaker (task 8.11, design §13.7): when open, skip the provider and go
        # straight to the deterministic template, so a repeatedly-failing Bedrock does not cost a
        # timeout per card. An accurate, non-generated summary is served — degraded, never blank.
        if self._breaker is not None and not self._breaker.allow():
            set_attr(SpanAttr.CLAIM_VALIDATION, "breaker_open")
            return self._fallback(request), self._provider.model_id, True, (0, 0)

        try:
            response = await self._chat(request)
        except Exception:
            self._trip()
            set_attr(SpanAttr.CLAIM_VALIDATION, "provider_error")
            return self._fallback(request), self._provider.model_id, True, (0, 0)
        self._recover()

        verdict = validate_claims(response.text, facts)
        if verdict.ok:
            set_attr(SpanAttr.CLAIM_VALIDATION, "pass")
            return response.text, response.model_id, False, _usage(response)

        # One retry with the violation named (design §8.4).
        retry = request.model_copy(
            update={
                "task": f"{request.task}\n\nA prior attempt was rejected: {verdict.summary()}. "
                "State only figures backed by an [F] fact, cited immediately."
            }
        )
        try:
            retried = await self._chat(retry)
        except Exception:
            self._trip()
            set_attr(SpanAttr.CLAIM_VALIDATION, "provider_error_on_retry")
            return self._fallback(request), self._provider.model_id, True, (0, 0)
        self._recover()

        if validate_claims(retried.text, facts).ok:
            set_attr(SpanAttr.CLAIM_VALIDATION, "pass_on_retry")
            return retried.text, retried.model_id, False, _usage(retried)

        set_attr(SpanAttr.CLAIM_VALIDATION, "fail")
        metrics = get_metrics()
        if metrics is not None:
            metrics.record_claim_rejection(agent=request.agent)
        return self._fallback(request), retried.model_id, True, _usage(retried)

    async def _chat(self, request: CompletionRequest) -> CompletionResponse:
        """One model call inside a ``gen_ai.chat`` span (GenAI conventions, task 8.10).

        Records the request model, max tokens, the response model and its token usage. No prompt or
        completion *content* is attached — that is gated behind ``OTEL_CAPTURE_PROMPT_CONTENT`` and
        the span-attribute allowlist drops it by default (design §13.4). Content belongs in the
        access-controlled audit log, not a telemetry backend.
        """
        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(f"chat {request.agent}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model", self._provider.model_id)
            span.set_attribute("gen_ai.request.max_tokens", request.max_tokens)
            response = await self._provider.complete(request)
            span.set_attribute("gen_ai.response.model", response.model_id)
            span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
            span.set_attribute(SpanAttr.OUTCOME, "ok")
            return response

    def _trip(self) -> None:
        """Record a generation failure against the breaker, if one is wired."""
        if self._breaker is not None:
            self._breaker.record_failure()

    def _recover(self) -> None:
        """Record a successful generation, closing the breaker, if one is wired."""
        if self._breaker is not None:
            self._breaker.record_success()

    def _fallback(self, request: CompletionRequest) -> str:
        """The deterministic template render — accurate, cited, non-generated (task 8.1/8.11)."""
        return self._renderer.render(request)


#: ``(input_tokens, output_tokens)`` carried back for the audit/cost path.
TokenUsageTuple = tuple[int, int]


def _usage(response: CompletionResponse) -> TokenUsageTuple:
    return response.usage.input_tokens, response.usage.output_tokens


def _fact_citations(facts: FactTable) -> tuple[FactCitation, ...]:
    return tuple(
        FactCitation(
            fact_id=fact.fact_id,
            entity_type=fact.entity_type,
            entity_id=fact.entity_id,
            field=fact.field,
        )
        for fact in facts.facts
    )


def _passage_citations(passages: Sequence[PassageRef]) -> tuple[PassageCitation, ...]:
    # PassageRef carries only the id/text/doc/section the prompt needed; the richer citation fields
    # (title, version, effective dates) are attached by load_context when it has the SectionRef, so
    # here we build from what the ref carries and leave the rest to be enriched upstream.
    return tuple(
        PassageCitation(
            passage_id=p.passage_id,
            doc_id=p.doc_id,
            section_path=p.section_path,
            title=p.doc_id,
            version="",
            effective_from="",
        )
        for p in passages
    )


__all__ = ["AgentInput", "AgentRunner"]
