"""Knowledge retrieval endpoints (task 7.8, design §6.3).

``GET /knowledge/search`` runs hybrid retrieval and returns cited passages; ``GET
/knowledge/documents/{doc_id}`` returns document metadata. Both derive entitlement from the
authenticated principal — the principal's knowledge levels are the access-level pre-filter — so a
role never retrieves, or learns of, a document above its entitlement (requirement 17.8).

Every retrieval writes ``retrieved_doc_ids`` to the audit log (requirement 12.6): what knowledge a
user was shown is auditable, the same way which customer fields they saw is. The write is
fail-closed — if the audit queue is saturated the request is refused rather than served unaudited —
so it goes through the same :func:`record_access` path the customer reads use.

When the knowledge base has not been ingested (or ``sqlite-vec`` is unavailable), the search
endpoint returns an empty, "no guidance found" result rather than an error: a missing base is a
degraded state, not a broken request. The document endpoint returns 404 for the same reason it does
for an unknown id — there is nothing to show.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from c360.api.audit import record_access
from c360.api.auth import current_principal
from c360.api.caching import conditional_response
from c360.api.envelope import ApiError, Envelope, ErrorCode
from c360.api.services import Services, require_services
from c360.knowledge.models import KnowledgeDomain
from c360.security.audit import AuditOutcome
from c360.security.model import Principal
from c360.tools.knowledge_tool import KnowledgePassage

router = APIRouter(tags=["knowledge"])


# ================================================================ response models
class KnowledgeSearchPayload(BaseModel):
    """``GET /knowledge/search`` payload: cited passages and retrieval counters (17.5, 17.6)."""

    model_config = ConfigDict(frozen=True)

    passages: tuple[KnowledgePassage, ...] = ()
    candidate_count: int = 0
    reranked: bool = False
    no_guidance_found: bool = False


class DocumentPayload(BaseModel):
    """``GET /knowledge/documents/{doc_id}`` payload: document metadata (design §6.3)."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    title: str
    domain: KnowledgeDomain
    version: str
    effective_from: str
    effective_to: str | None = None
    jurisdiction: str | None = None
    access_level: str
    product_code: str | None = None
    business_group: str | None = None


# ================================================================ search
@router.get(
    "/knowledge/search",
    response_model=Envelope[KnowledgeSearchPayload],
    summary="Hybrid knowledge retrieval with citations",
    responses={503: {"description": "Audit unavailable (fail-closed)"}},
)
async def knowledge_search(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    q: Annotated[str, Query(min_length=1, description="Retrieval query (no PII).")],
    domain: Annotated[
        list[KnowledgeDomain] | None, Query(description="Restrict to these domains.")
    ] = None,
    product_code: Annotated[str | None, Query(description="Narrow to one product code.")] = None,
) -> Envelope[KnowledgeSearchPayload]:
    """Retrieve passages for ``q``, entitlement-filtered, and audit the documents returned.

    Reranking is not enabled on this endpoint: it is the search surface, held to the dashboard-agent
    budget. The Q&A path (Phase 9) enables reranking on its own route.
    """
    service = services.knowledge
    domains = tuple(domain) if domain else ()

    if service is None:
        # Degraded: no knowledge base. Still audit the (empty) retrieval so the attempt is recorded.
        _audit(request, principal, query_present=True, doc_ids=())
        return Envelope.of(KnowledgeSearchPayload(no_guidance_found=True))

    result = await run_in_threadpool(
        service.search, principal, q, domains=domains, product_code=product_code
    )
    passages = tuple(KnowledgePassage.of(passage) for passage in result.passages)
    doc_ids = _dedupe(passage.doc_id for passage in passages)

    # Fail-closed audit before returning the passages (requirement 12.6).
    _audit(request, principal, query_present=True, doc_ids=doc_ids)

    return Envelope.of(
        KnowledgeSearchPayload(
            passages=passages,
            candidate_count=result.candidate_count,
            reranked=result.reranked,
            no_guidance_found=result.is_empty,
        )
    )


# ================================================================ document
@router.get(
    "/knowledge/documents/{doc_id}",
    response_model=Envelope[DocumentPayload],
    summary="Document metadata",
    responses={
        304: {"description": "Not modified (client's ETag matches)"},
        404: {"description": "Unknown document or not entitled"},
    },
)
async def get_document(
    request: Request,
    doc_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
    version: Annotated[str | None, Query(description="A specific version, or latest.")] = None,
) -> Response:
    """Return document metadata, or 404 if unknown or above the role's entitlement (17.8).

    Absent and forbidden are collapsed into 404, so the endpoint is not an existence oracle for
    restricted documents — the same discipline the customer endpoints apply.

    Document metadata is institutional reference data (task 16.2): it changes only on re-ingestion,
    not per request or per role. The response therefore carries a strong ``ETag`` and a short
    ``Cache-Control`` so a repeat read can be a conditional ``304`` rather than a full round trip.
    """
    service = services.knowledge
    if service is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "document not found")

    meta = await run_in_threadpool(service.get_document, principal, doc_id, version)
    if meta is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "document not found")

    envelope = Envelope.of(
        DocumentPayload(
            doc_id=meta.doc_id,
            title=meta.title,
            domain=meta.domain,
            version=meta.version,
            effective_from=meta.effective_from,
            effective_to=meta.effective_to,
            jurisdiction=meta.jurisdiction,
            access_level=meta.access_level,
            product_code=meta.product_code,
            business_group=meta.business_group,
        )
    )
    return conditional_response(request, envelope, etag_source=envelope.data)


# ================================================================ helpers
def _audit(
    request: Request,
    principal: Principal,
    *,
    query_present: bool,
    doc_ids: tuple[str, ...],
) -> None:
    """Write the retrieval to the audit log, failing the request closed if the queue is full."""
    record_access(
        request.app.state.audit_writer,
        principal,
        action="KNOWLEDGE_SEARCH",
        outcome=AuditOutcome.ALLOWED,
        request_path=str(request.url.path),
        retrieved_doc_ids=doc_ids or None,
    )
    _ = query_present  # the query text itself is never audited — only which documents were returned


def _dedupe(doc_ids: object) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for doc_id in doc_ids:  # type: ignore[attr-defined]
        seen.setdefault(str(doc_id), None)
    return tuple(seen)


__all__ = ["router"]
