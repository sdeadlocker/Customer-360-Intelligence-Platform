"""Liveness and readiness endpoints (design §6.3, §13.8)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from c360 import __version__
from c360.api.envelope import Envelope
from c360.api.readiness import REGISTRY, CheckStatus

router = APIRouter(tags=["operations"])


class HealthPayload(BaseModel):
    """Liveness payload. Deliberately dependency-free."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    version: str = __version__


class ComponentPayload(BaseModel):
    """One readiness component."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: CheckStatus
    detail: str = ""


class ReadyPayload(BaseModel):
    """Readiness payload: the aggregate plus every component's outcome."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]
    version: str = __version__
    components: list[ComponentPayload]


@router.get(
    "/health",
    response_model=Envelope[HealthPayload],
    summary="Liveness probe",
)
async def health() -> Envelope[HealthPayload]:
    """Return 200 while the process is alive. Touches no dependency."""
    return Envelope.of(HealthPayload())


@router.get(
    "/ready",
    response_model=Envelope[ReadyPayload],
    summary="Readiness probe",
    responses={503: {"description": "One or more components are not ready"}},
)
async def ready(response: Response) -> Envelope[ReadyPayload]:
    """Report readiness of every registered subsystem, failing closed on any blocking check."""
    is_ready, results = REGISTRY.run()
    if not is_ready:
        response.status_code = 503

    return Envelope.of(
        ReadyPayload(
            status="ready" if is_ready else "not_ready",
            components=[
                ComponentPayload(name=result.name, status=result.status, detail=result.detail)
                for result in results
            ],
        )
    )
