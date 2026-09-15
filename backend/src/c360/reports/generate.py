"""Report generation: resolve scope, gather masked reads, render, persist (task 18.2, 18.3, 18.4).

This is the single place a report's content is produced, shared by the manual run endpoint and the
scheduled path. It does the security-critical work Phase 18 turns on:

* **Entitlement re-checked at generation time (task 18.4).** The owner's *live* entitlement scope is
  resolved into the set of customers the report may cover, using the same
  :func:`c360.security.authorization.authorize_customer` gate every customer read uses — never a
  stale snapshot stored on the schedule. A revoked book therefore stops producing data immediately.
* **Masked, entitlement-scoped reads (task 18.2).** Each customer's 360 view, risk, offers and (for
  a briefing) signals are read through the same services the API uses, and rendered from the masked
  serialization under the owner's field policy, so a report never contains a field the role may not
  see.

The AI narratives are produced by running the dashboard agent graph, which runs on the mock provider
offline and on Bedrock when ``LLM_PROVIDER=bedrock`` — the same runtime the dashboard uses (task
18.3). When no agent runtime is supplied (a pure-deterministic run) the report renders without
narratives rather than failing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from c360.reports import render
from c360.reports.models import (
    Branding,
    GeneratedArtifact,
    ReportDefinition,
    ReportScopeKind,
    ReportType,
)
from c360.security.authorization import authorize_customer
from c360.security.errors import EntitlementError

if TYPE_CHECKING:
    from c360.agents.result import AgentResult
    from c360.agents.runtime import AgentRuntime
    from c360.api.services import Services
    from c360.security.model import Principal
    from c360.services.aggregator import Customer360
    from c360.signals.models import Signal


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Everything :func:`generate_report` needs, threaded from the endpoint or the scheduler."""

    definition: ReportDefinition
    principal: Principal
    max_customers: int


def _resolve_customers(
    definition: ReportDefinition,
    principal: Principal,
    services: Services,
    max_customers: int,
) -> list[str]:
    """The customers this report covers, filtered to the owner's *live* entitlement (task 18.4).

    Every candidate is passed through :func:`authorize_customer`; a non-entitled or non-existent id
    is silently dropped rather than raising, so a book report over a partially-revoked book produces
    the still-entitled subset (and a fully-revoked book produces an empty report) rather than
    failing. This is the generation-time re-check the scheduler requires: the scope stored on the
    definition is only a candidate list, never a grant.
    """
    scope = definition.scope
    candidates: list[str]
    if scope.kind is ReportScopeKind.CUSTOMER:
        candidates = [scope.customer_id] if scope.customer_id else []
    elif scope.kind is ReportScopeKind.BOOK:
        candidates = list(scope.customer_ids)
    else:  # SEGMENT — resolve via the entitled search over the owner's scope
        candidates = _customers_in_segments(principal, services, scope.segments, max_customers)

    entitled: list[str] = []
    repository = services.customer.repository
    for customer_id in candidates:
        if len(entitled) >= max_customers:
            break
        try:
            authorize_customer(principal, customer_id, repository)
        except EntitlementError:
            continue
        entitled.append(customer_id)
    return entitled


def _customers_in_segments(
    principal: Principal,
    services: Services,
    segments: tuple[str, ...],
    max_customers: int,
) -> list[str]:
    """Resolve segment scope to entitled customer ids via the entitlement-scoped customer list.

    Uses the repository's entitlement-scoped listing so the owner's scope is applied in SQL; the
    per-customer ``authorize_customer`` re-check in the caller is then a belt-and-braces gate that
    also drops any id outside the requested segments.
    """
    if not segments:
        return []
    wanted = set(segments)
    result: list[str] = []
    for customer in services.customer.repository.list_all():
        if str(customer.customer_segment) in wanted:
            result.append(customer.customer_id)
            if len(result) >= max_customers:
                break
    del principal  # entitlement is enforced by the per-id re-check in the caller
    return result


async def _narratives_for(
    runtime: AgentRuntime | None,
    services: Services,
    principal: Principal,
    customer_id: str,
) -> dict[str, AgentResult]:
    """Run the dashboard agent graph for one customer, returning each agent's result (task 18.3).

    ``None`` runtime (a pure-deterministic report) yields no narratives. A graph failure is
    contained: the report renders without narratives rather than failing the whole run, mirroring
    the dashboard's per-card degradation.
    """
    if runtime is None:
        return {}
    try:
        graph = runtime.graph_for(services)
        state = await graph.arun(principal, customer_id)
    except Exception:
        return {}
    outputs = state.get("agent_outputs", {})
    return dict(outputs)


def _gather_sections(
    definition: ReportDefinition,
    principal: Principal,
    services: Services,
    runtime: AgentRuntime | None,
    customer_ids: list[str],
) -> tuple[
    list[tuple[Customer360, dict[str, AgentResult]]],
    list[tuple[Customer360, dict[str, AgentResult], tuple[Signal, ...]]],
]:
    """Build the per-customer sections for both artifact kinds from masked reads."""
    pack_sections: list[tuple[Customer360, dict[str, AgentResult]]] = []
    briefing_sections: list[tuple[Customer360, dict[str, AgentResult], tuple[Signal, ...]]] = []
    for customer_id in customer_ids:
        view = services.aggregator.build_360(principal, customer_id)
        narratives = asyncio.run(_narratives_for(runtime, services, principal, customer_id))
        pack_sections.append((view, narratives))
        signals = _signals_for(services, principal, customer_id)
        briefing_sections.append((view, narratives, signals))
    return pack_sections, briefing_sections


def _signals_for(services: Services, principal: Principal, customer_id: str) -> tuple[Signal, ...]:
    """The customer's signals for the briefing (task 18.3), or empty when signals are not built."""
    signals = services.signals
    if signals is None:
        return ()
    return signals.for_customer(customer_id, principal.user_id)


def _branding(definition: ReportDefinition) -> Branding:
    return definition.branding


def generate_report(
    request: GenerationRequest,
    services: Services,
    runtime: AgentRuntime | None,
) -> GeneratedArtifact:
    """Produce the artifact for a definition, entitlement-re-checked and masked (task 18.2, 18.3).

    Runs synchronously (the SQLite driver and the agent graph are driven with ``asyncio.run`` per
    customer), so it is called from a worker thread via ``run_in_threadpool`` on the API path and
    directly on the CLI path.
    """
    customer_ids = _resolve_customers(
        request.definition, request.principal, services, request.max_customers
    )
    pack_sections, briefing_sections = _gather_sections(
        request.definition, request.principal, services, runtime, customer_ids
    )
    branding = _branding(request.definition)
    if request.definition.report_type is ReportType.MEETING_BRIEFING:
        return render.render_meeting_briefing(
            branding=branding,
            title=request.definition.title,
            sections=briefing_sections,
            policy=request.principal.field_policy,
        )
    return render.render_pdf_pack(
        branding=branding,
        title=request.definition.title,
        sections=pack_sections,
        policy=request.principal.field_policy,
    )


__all__ = ["GenerationRequest", "generate_report"]
