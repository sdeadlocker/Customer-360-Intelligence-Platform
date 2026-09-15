"""``GET /me`` — the caller's principal, role and entitlement summary (design §6.3).

A tiny endpoint the UI calls on load to learn who it is talking to: the role drives which
affordances
render, and the entitlement summary tells the client whether it is looking at a whole book or a
segment. It exposes nothing about *other* principals and no customer data, so it needs no masking —
only the caller's own identity, which they already hold as a token.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from c360.api.auth import current_principal
from c360.api.envelope import Envelope
from c360.security.entitlement import AllScope, BookScope
from c360.security.model import Principal

router = APIRouter(tags=["identity"])


class EntitlementSummary(BaseModel):
    """A non-identifying description of the caller's entitlement scope.

    ``kind`` is ``ALL`` / ``BOOK`` / ``SEGMENT``; ``count`` is the size of a book or the number of
    segments (``None`` for ``ALL``). The book's customer ids themselves are not returned — the
    client
    does not need them and echoing a whole book back on every load would be needless exposure.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    count: int | None = None
    segments: tuple[str, ...] = ()


class MeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    role: str
    entitlement: EntitlementSummary
    knowledge_levels: tuple[str, ...] = ()


def _summarize(principal: Principal) -> EntitlementSummary:
    scope = principal.entitlement
    if isinstance(scope, AllScope):
        return EntitlementSummary(kind="ALL")
    if isinstance(scope, BookScope):
        return EntitlementSummary(kind="BOOK", count=len(scope.customer_ids))
    segments = tuple(sorted(str(segment) for segment in scope.segments))
    return EntitlementSummary(kind="SEGMENT", count=len(segments), segments=segments)


@router.get("/me", response_model=Envelope[MeResponse], summary="The caller's identity")
async def get_me(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Envelope[MeResponse]:
    return Envelope.of(
        MeResponse(
            user_id=principal.user_id,
            role=str(principal.role),
            entitlement=_summarize(principal),
            knowledge_levels=tuple(sorted(str(level) for level in principal.knowledge_levels)),
        )
    )


__all__ = ["router"]
