"""Report export endpoints (task 18.5).

The REST surface for Phase 18: define a report, run it, list a definition's runs, download a run's
artifact, and manage schedules. Every route is authenticated, owner-scoped (a caller sees and runs
only reports they own) and audited. Generation itself re-checks the owner's live entitlement per
customer and reads through the masked services (task 18.2, 18.4), so a report never contains data
the requesting role may not see.

Blocking work — the SQLite writes and the synchronous generation over the agent graph — runs off the
event loop via ``run_in_threadpool``, matching the concurrency model the rest of the API uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from c360.api.audit import record_access
from c360.api.auth import current_principal
from c360.api.envelope import ApiError, Envelope, ErrorCode
from c360.api.services import Services, require_services
from c360.reports.models import (
    Branding,
    ReportRun,
    ReportScope,
    ReportScopeKind,
    ReportType,
)
from c360.security.audit import AuditOutcome
from c360.security.model import Principal

if TYPE_CHECKING:
    from c360.agents.runtime import AgentRuntime
    from c360.reports.service import ReportService

router = APIRouter(tags=["reports"])


# ==================================================================== request/response models
class ScopeBody(BaseModel):
    """The scope a report runs over (task 18.1)."""

    model_config = ConfigDict(frozen=True)

    kind: ReportScopeKind
    customer_id: str | None = None
    customer_ids: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()

    def to_scope(self) -> ReportScope:
        return ReportScope(
            kind=self.kind,
            customer_id=self.customer_id,
            customer_ids=tuple(self.customer_ids),
            segments=tuple(self.segments),
        )


class BrandingBody(BaseModel):
    """Per-definition branding (task 18.1). Falls back to platform defaults when omitted."""

    model_config = ConfigDict(frozen=True)

    brand_name: str | None = None
    tagline: str | None = None


class DefineReportBody(BaseModel):
    """The body of ``POST /reports`` (task 18.5)."""

    model_config = ConfigDict(frozen=True)

    report_type: ReportType
    title: str = Field(min_length=1, max_length=200)
    scope: ScopeBody
    branding: BrandingBody = BrandingBody()


class ScheduleBody(BaseModel):
    """The body of ``POST /reports/{id}/schedules`` (task 18.5)."""

    model_config = ConfigDict(frozen=True)

    cadence: str = Field(pattern=r"^(DAILY|WEEKLY|MONTHLY)$")


class DefinitionCreated(BaseModel):
    model_config = ConfigDict(frozen=True)

    definition_id: int


class ScheduleCreated(BaseModel):
    model_config = ConfigDict(frozen=True)

    schedule_id: int
    definition_id: int
    cadence: str


class RunView(BaseModel):
    """A run's status and provenance, value-free (task 18.5)."""

    model_config = ConfigDict(frozen=True)

    run_id: int
    definition_id: int
    report_type: str
    status: str
    trigger_kind: str
    customers_rendered: int
    model_ids: tuple[str, ...]
    prompt_versions: tuple[str, ...]
    degraded: bool
    started_at: str
    finished_at: str | None
    error: str | None


class RunList(BaseModel):
    model_config = ConfigDict(frozen=True)

    runs: tuple[RunView, ...]


def _run_view(run: ReportRun) -> RunView:
    return RunView(
        run_id=run.run_id,
        definition_id=run.definition_id,
        report_type=str(run.report_type),
        status=str(run.status),
        trigger_kind=run.trigger_kind,
        customers_rendered=run.customers_rendered,
        model_ids=run.provenance.model_ids,
        prompt_versions=run.provenance.prompt_versions,
        degraded=run.provenance.degraded,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
    )


# ==================================================================== helpers
def _require_reports(services: Services) -> ReportService:
    reports = services.reports
    if reports is None:
        raise ApiError(ErrorCode.UPSTREAM_UNAVAILABLE, "the report subsystem is not available")
    return reports


def _agent_runtime(request: Request) -> AgentRuntime | None:
    """The process-wide agent runtime, or ``None`` when the platform runs without the AI layer."""
    runtime: AgentRuntime | None = getattr(request.app.state, "agent_runtime", None)
    return runtime


def _audit(request: Request, principal: Principal, *, action: str, outcome: AuditOutcome) -> None:
    record_access(
        request.app.state.audit_writer,
        principal,
        action=action,
        outcome=outcome,
        request_path=str(request.url.path),
    )


# ==================================================================== define
@router.post(
    "/reports",
    response_model=Envelope[DefinitionCreated],
    summary="Define a report",
)
async def define_report(
    body: DefineReportBody,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[DefinitionCreated]:
    """Create a report definition owned by the caller (task 18.5)."""
    reports = _require_reports(services)
    settings = request.app.state.settings
    branding = Branding(
        brand_name=body.branding.brand_name or settings.reports_brand_name,
        tagline=(
            body.branding.tagline
            if body.branding.tagline is not None
            else settings.reports_brand_tagline
        ),
    )
    _audit(request, principal, action="REPORT_DEFINE", outcome=AuditOutcome.ALLOWED)
    definition_id = await run_in_threadpool(
        reports.create_definition,
        report_type=body.report_type,
        title=body.title,
        scope=body.scope.to_scope(),
        branding=branding,
        owner=principal,
    )
    return Envelope.of(DefinitionCreated(definition_id=definition_id))


# ==================================================================== run
@router.post(
    "/reports/{definition_id}/run",
    response_model=Envelope[RunView],
    summary="Run a report now",
    responses={404: {"description": "No such report, or not owned by the caller"}},
)
async def run_report(
    definition_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[RunView]:
    """Generate the report now, entitlement re-checked and masked (task 18.2, 18.5)."""
    reports = _require_reports(services)
    definition = await run_in_threadpool(reports.get_definition, definition_id, principal)
    if definition is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such report")
    _audit(request, principal, action="REPORT_RUN", outcome=AuditOutcome.ALLOWED)
    run = await run_in_threadpool(
        reports.run_definition,
        definition,
        principal,
        services,
        _agent_runtime(request),
        trigger_kind="MANUAL",
    )
    return Envelope.of(_run_view(run))


# ==================================================================== run history
@router.get(
    "/reports/{definition_id}/runs",
    response_model=Envelope[RunList],
    summary="A report's run history",
    responses={404: {"description": "No such report, or not owned by the caller"}},
)
async def list_report_runs(
    definition_id: int,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[RunList]:
    """Every run of a definition the caller owns, newest first (task 18.5)."""
    reports = _require_reports(services)
    definition = await run_in_threadpool(reports.get_definition, definition_id, principal)
    if definition is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such report")
    runs = await run_in_threadpool(reports.list_runs, definition_id, principal)
    return Envelope.of(RunList(runs=tuple(_run_view(run) for run in runs)))


# ==================================================================== download
@router.get(
    "/reports/runs/{run_id}",
    summary="Download a run's artifact",
    responses={
        200: {"content": {"application/pdf": {}}},
        404: {"description": "No such run, not owned by the caller, or no artifact"},
    },
)
async def download_run(
    run_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> FileResponse:
    """Stream the generated artifact for a run the caller owns (task 18.5)."""
    reports = _require_reports(services)
    run = await run_in_threadpool(reports.get_run, run_id, principal)
    if run is None or run.artifact_path is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such run artifact")
    path = Path(run.artifact_path)
    if not path.is_file():
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "artifact no longer available")
    _audit(request, principal, action="REPORT_DOWNLOAD", outcome=AuditOutcome.ALLOWED)
    return FileResponse(path, media_type="application/pdf", filename=path.name)


# ==================================================================== schedules
@router.post(
    "/reports/{definition_id}/schedules",
    response_model=Envelope[ScheduleCreated],
    summary="Attach a schedule to a report",
    responses={404: {"description": "No such report, or not owned by the caller"}},
)
async def create_schedule(
    definition_id: int,
    body: ScheduleBody,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[ScheduleCreated]:
    """Attach a cron-like cadence to a report the caller owns (task 18.4, 18.5)."""
    reports = _require_reports(services)
    definition = await run_in_threadpool(reports.get_definition, definition_id, principal)
    if definition is None:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such report")
    _audit(request, principal, action="REPORT_SCHEDULE", outcome=AuditOutcome.ALLOWED)
    schedule_id = await run_in_threadpool(
        reports.create_schedule,
        definition_id=definition_id,
        cadence=body.cadence,
        owner=principal,
    )
    return Envelope.of(
        ScheduleCreated(schedule_id=schedule_id, definition_id=definition_id, cadence=body.cadence)
    )


@router.delete(
    "/reports/schedules/{schedule_id}",
    response_model=Envelope[dict[str, bool]],
    summary="Disable a schedule",
    responses={404: {"description": "No such schedule"}},
)
async def disable_schedule(
    schedule_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(require_services)],
) -> Envelope[dict[str, bool]]:
    """Deactivate a schedule so it stops running (task 18.5)."""
    reports = _require_reports(services)
    _audit(request, principal, action="REPORT_UNSCHEDULE", outcome=AuditOutcome.ALLOWED)
    ok = await run_in_threadpool(reports.set_schedule_active, schedule_id, active=False)
    if not ok:
        raise ApiError(ErrorCode.CUSTOMER_NOT_FOUND, "no such schedule")
    return Envelope.of({"disabled": True})


__all__ = ["router"]
