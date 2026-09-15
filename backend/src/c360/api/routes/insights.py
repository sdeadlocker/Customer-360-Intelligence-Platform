"""Streaming AI dashboard endpoints (task 8.9, design §8.1).

``GET /customers/{id}/insights`` and ``GET /customers/{id}/recommendations`` run the dashboard agent
graph and stream each agent's card to the client as a server-sent event the moment that agent
finishes, so a fast card is not gated on the slowest one (design §8.1). Both endpoints run the same
single graph — the summary agent depends on every wave regardless — and differ only in which agents'
events they forward: ``recommendations`` forwards the offer agent, ``insights`` forwards the rest.

Both apply the same authorization gate as every customer read (the 403-vs-404 discipline) *before*
streaming begins, and audit the AI access fail-closed, so the streaming path is no more permissive
than the REST path. The per-card timeout lives inside the graph (task 8.6): a slow card degrades on
its own without truncating the stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from c360.agents.runtime import AgentRuntime
from c360.agents.streaming import stream_dashboard
from c360.api.audit import record_access
from c360.api.auth import current_principal
from c360.api.services import Services, require_services
from c360.security.audit import AuditOutcome
from c360.security.authorization import authorize_customer
from c360.security.errors import EntitlementError
from c360.security.model import Principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(tags=["insights"])

#: The offer agent's card is the "recommendations" surface; everything else is "insights".
_RECOMMENDATION_AGENTS = frozenset({"offer_recommendation"})
_INSIGHT_AGENTS = frozenset(
    {"financial_health", "risk", "life_event", "relationship", "journey", "customer_summary"}
)

_SSE_MEDIA_TYPE = "text/event-stream"


def require_agent_runtime(request: Request) -> AgentRuntime:
    """FastAPI dependency: the process-wide agent runtime, or a 503 if the app has no AI layer.

    The runtime is stateless and built at app construction, so its absence means the app was built
    without the agent layer — an operational misconfiguration, surfaced as 503 rather than a 500.
    """
    from c360.api.envelope import ApiError, ErrorCode  # noqa: PLC0415 - avoid a route import cycle

    runtime: AgentRuntime | None = getattr(request.app.state, "agent_runtime", None)
    if runtime is None:
        raise ApiError(ErrorCode.UPSTREAM_UNAVAILABLE, "the AI layer is not available")
    return runtime


@router.get(
    "/customers/{customer_id}/insights",
    summary="Stream the AI insight cards as server-sent events",
    responses={200: {"content": {_SSE_MEDIA_TYPE: {}}}},
)
async def stream_insights(
    customer_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    runtime: Annotated[AgentRuntime, Depends(require_agent_runtime)],
) -> StreamingResponse:
    """Stream every insight card (all agents except the offer recommendation) over SSE."""
    return await _stream(request, principal, services, runtime, customer_id, _INSIGHT_AGENTS)


@router.get(
    "/customers/{customer_id}/recommendations",
    summary="Stream the AI recommendation card as server-sent events",
    responses={200: {"content": {_SSE_MEDIA_TYPE: {}}}},
)
async def stream_recommendations(
    customer_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    runtime: Annotated[AgentRuntime, Depends(require_agent_runtime)],
) -> StreamingResponse:
    """Stream the next-best-offer recommendation card over SSE."""
    return await _stream(request, principal, services, runtime, customer_id, _RECOMMENDATION_AGENTS)


async def _stream(
    request: Request,
    principal: Principal,
    services: Services,
    runtime: AgentRuntime,
    customer_id: str,
    only: frozenset[str],
) -> StreamingResponse:
    """Authorize, audit, then return the SSE stream of the selected agents' cards."""
    await run_in_threadpool(_authorize, principal, services, customer_id)
    # Fail-closed audit of the AI access before any card is streamed (requirement 12.6).
    record_access(
        request.app.state.audit_writer,
        principal,
        action="AI_DASHBOARD",
        outcome=AuditOutcome.ALLOWED,
        customer_id=customer_id,
        request_path=str(request.url.path),
    )
    graph = runtime.graph_for(services, audit_sink=request.app.state.audit_writer)

    async def _body() -> AsyncIterator[str]:
        async for frame in stream_dashboard(graph, principal, customer_id, only=only):
            yield frame

    return StreamingResponse(
        _body(),
        media_type=_SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _authorize(principal: Principal, services: Services, customer_id: str) -> None:
    """Collapse non-entitled and non-existent into 404, exactly as the customer REST routes do."""
    from c360.api.envelope import ApiError, ErrorCode  # noqa: PLC0415 - avoid a route import cycle

    try:
        authorize_customer(principal, customer_id, services.customer.repository)
    except EntitlementError as error:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "customer not found") from error


__all__ = ["require_agent_runtime", "router"]
