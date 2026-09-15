"""Grounded natural-language Q&A over SSE (task 9.5/9.6, design §6.3).

Two entry points share the one ReAct graph (Phase 9), which reasons over the same typed tools the
REST API uses and streams its answer back as server-sent events:

* ``POST /customers/{id}/ask`` — the single-customer path, used from a customer's dashboard. It
  applies the same 403-vs-404 authorization gate every customer read applies, audits the AI access
  fail-closed *before* answering, threads conversation memory by ``(session_id, customer_id)``
  (deleting a prior customer's thread on a switch), runs the graph, audits a refusal as a denied
  access, and then streams the result.
* ``POST /ask`` — the cross-customer path, the "Ask anything" surface on the search landing page.
  No customer is preselected, so there is no up-front ``authorize_customer`` gate; instead the agent
  resolves the customer itself with the entitlement-scoped ``customer_search`` tool, and every
  reading tool re-authorizes the id it resolves to. Entitlement is therefore enforced per result,
  exactly as on the dashboard path, never bypassed. The access is still audited fail-closed before
  answering (with no ``customer_id``, since none is known yet), and a refusal is audited as denied.

Rerank is enabled on both paths (task 9.5): the Q&A loop passes ``rerank=True`` to the
``knowledge_search`` tool, so the extra ranking latency is spent where answer quality matters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from c360.agents.qa_runtime import search_thread_id, thread_id
from c360.agents.qa_streaming import stream_qa
from c360.agents.runtime import AgentRuntime
from c360.api.audit import record_access
from c360.api.auth import current_principal
from c360.api.routes.insights import require_agent_runtime
from c360.api.services import Services, require_services
from c360.security.audit import AuditOutcome
from c360.security.authorization import authorize_customer
from c360.security.errors import EntitlementError
from c360.security.model import Principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(tags=["ask"])

_SSE_MEDIA_TYPE = "text/event-stream"


class AskRequest(BaseModel):
    """The Q&A request body: a question and the session it belongs to (design §10.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(
        min_length=1, max_length=2000, description="The natural-language question."
    )
    session_id: str = Field(
        min_length=1,
        max_length=128,
        description="Conversation session; memory is keyed by (session_id, customer_id).",
    )


@router.post(
    "/customers/{customer_id}/ask",
    summary="Ask a grounded natural-language question about a customer (SSE)",
    responses={200: {"content": {_SSE_MEDIA_TYPE: {}}}},
)
async def ask(
    customer_id: str,
    body: AskRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    runtime: Annotated[AgentRuntime, Depends(require_agent_runtime)],
) -> StreamingResponse:
    """Authorize, audit, run the Q&A graph, and stream the grounded answer (task 9.5)."""
    await run_in_threadpool(_authorize, principal, services, customer_id)
    # Fail-closed audit of the Q&A access before any answer is produced (requirement 12.6).
    record_access(
        request.app.state.audit_writer,
        principal,
        action="AI_QA",
        outcome=AuditOutcome.ALLOWED,
        customer_id=customer_id,
        request_path=str(request.url.path),
    )

    # Drop any thread this session held for another customer before answering (requirement 11.9).
    await runtime.memory.switch_customer(body.session_id, customer_id)
    graph = await runtime.qa_graph_for(services)
    state: dict[str, Any] = dict(
        await graph.arun(
            principal,
            customer_id,
            body.question,
            thread_id=thread_id(body.session_id, customer_id),
        )
    )

    # A refusal inside the loop (a tool reached a non-entitled customer) is audited as denied
    # access, disclosing nothing beyond the refusal already on the state (requirement 11.6).
    if state.get("refused"):
        record_access(
            request.app.state.audit_writer,
            principal,
            action="AI_QA_REFUSED",
            outcome=AuditOutcome.DENIED,
            customer_id=customer_id,
            request_path=str(request.url.path),
        )

    _record_qa_metrics(state)

    async def _body() -> AsyncIterator[str]:
        async for frame in stream_qa(state, model_id=runtime.provider.model_id):
            yield frame

    return StreamingResponse(
        _body(),
        media_type=_SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/ask",
    summary="Ask a grounded natural-language question across all entitled customers (SSE)",
    responses={200: {"content": {_SSE_MEDIA_TYPE: {}}}},
)
async def ask_all(
    body: AskRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    runtime: Annotated[AgentRuntime, Depends(require_agent_runtime)],
) -> StreamingResponse:
    """Answer a cross-customer question and stream the grounded answer (task 9.6).

    The search-landing "Ask anything" entry point. There is no customer to authorize up front, so
    the agent resolves one with the entitlement-scoped ``customer_search`` tool and every reading
    tool re-authorizes the id it resolves to — entitlement is enforced per result, not skipped. The
    access is audited fail-closed before answering, and a refusal (a tool that reached a
    non-entitled customer) is audited as a denied access, disclosing nothing.
    """
    # Fail-closed audit of the Q&A access before any answer is produced (requirement 12.6). No
    # customer_id: none is known until the agent resolves one, and the per-result reads it then
    # makes are each authorized and audited on their own path.
    record_access(
        request.app.state.audit_writer,
        principal,
        action="AI_QA",
        outcome=AuditOutcome.ALLOWED,
        request_path=str(request.url.path),
    )

    graph = await runtime.qa_graph_for(services)
    state: dict[str, Any] = dict(
        await graph.arun(
            principal,
            None,
            body.question,
            thread_id=search_thread_id(body.session_id),
        )
    )

    if state.get("refused"):
        record_access(
            request.app.state.audit_writer,
            principal,
            action="AI_QA_REFUSED",
            outcome=AuditOutcome.DENIED,
            request_path=str(request.url.path),
        )

    _record_qa_metrics(state)

    async def _body() -> AsyncIterator[str]:
        async for frame in stream_qa(state, model_id=runtime.provider.model_id):
            yield frame

    return StreamingResponse(
        _body(),
        media_type=_SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _record_qa_metrics(state: dict[str, Any]) -> None:
    """Emit the Q&A behaviour metrics (task 10.2, design §13.3 Q&A row).

    Tool calls per question, refusals by reason and clarifications — the three the design names.
    A refusal is labelled ``entitlement`` when a tool hit a non-entitled customer or ``no_guidance``
    when retrieval returned nothing; both are bounded, non-identifying reasons. The counters are
    what the denied-access and behaviour panels burn on, never the question text.
    """
    from c360.core.telemetry import get_metrics  # noqa: PLC0415 - avoid a route import cycle

    metrics = get_metrics()
    if metrics is None:
        return
    refused_reason: str | None = None
    if state.get("refused"):
        refused_reason = "entitlement"
    elif state.get("no_guidance"):
        refused_reason = "no_guidance"
    metrics.record_qa(
        tool_calls=int(state.get("tool_calls_made", 0) or 0),
        refused_reason=refused_reason,
        clarified=bool(state.get("clarified", False)),
    )


def _authorize(principal: Principal, services: Services, customer_id: str) -> None:
    """Collapse non-entitled and non-existent into 404, exactly as the customer REST routes do."""
    from c360.api.envelope import ApiError, ErrorCode  # noqa: PLC0415 - avoid a route import cycle

    try:
        authorize_customer(principal, customer_id, services.customer.repository)
    except EntitlementError as error:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "customer not found") from error


__all__ = ["AskRequest", "ask", "ask_all", "router"]
