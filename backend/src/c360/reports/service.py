"""The report service: define, run, schedule and read report history (task 18.4, 18.5).

Sits between the routes/CLI and the store/repository/generator. It owns the run lifecycle — open a
run row, resolve the owner's live entitlement, gather masked reads, render, write the artifact,
deliver, and close the run with its provenance — and the scheduled batch that runs every due
schedule. The security-critical entitlement re-check lives in :mod:`c360.reports.generate`; this
layer wires it to persistence, delivery and telemetry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from c360.core.logging import get_logger
from c360.data.engine import AccessMode, DatabaseFileMissingError, create_sqlite_engine
from c360.reports import telemetry
from c360.reports.delivery import ArtifactStore, Deliverer, FileDeliverer
from c360.reports.generate import GenerationRequest, generate_report
from c360.reports.models import (
    Branding,
    ReportDefinition,
    ReportRun,
    ReportSchedule,
    ReportScope,
    ReportType,
    RunProvenance,
    RunStatus,
)
from c360.reports.repository import ReportRepository
from c360.reports.store import ReportStore, initialize_reports_db, now_utc_date

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from c360.agents.runtime import AgentRuntime
    from c360.api.services import Services
    from c360.security.model import Principal

_logger = get_logger(__name__)

#: Minimum days between runs for each cadence, for the scheduler's due-check (task 18.4).
_CADENCE_DAYS: dict[str, int] = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 30}


@dataclass(frozen=True, slots=True)
class ScheduledRunSummary:
    """What one scheduled batch produced, returned for the CLI and asserted on by tests."""

    schedules_due: int
    runs_started: int
    runs_succeeded: int
    runs_failed: int

    def summary_lines(self) -> list[str]:
        return [
            f"schedules_due {self.schedules_due:>9,}",
            f"started       {self.runs_started:>9,}",
            f"succeeded     {self.runs_succeeded:>9,}",
            f"failed        {self.runs_failed:>9,}",
        ]


class ReportService:
    """Define, run, schedule and read reports (task 18.4, 18.5)."""

    __slots__ = (
        "_artifacts",
        "_deliverer",
        "_max_customers",
        "_repository",
        "_store",
    )

    def __init__(
        self,
        repository: ReportRepository,
        store: ReportStore,
        *,
        artifacts: ArtifactStore,
        deliverer: Deliverer,
        max_customers: int,
    ) -> None:
        self._repository = repository
        self._store = store
        self._artifacts = artifacts
        self._deliverer = deliverer
        self._max_customers = max_customers

    # ---------------------------------------------------------------- definitions & schedules
    def create_definition(
        self,
        *,
        report_type: ReportType,
        title: str,
        scope: ReportScope,
        branding: Branding,
        owner: Principal,
    ) -> int:
        """Create a report definition owned by the caller (task 18.5)."""
        return self._store.create_definition(
            report_type=report_type,
            title=title,
            scope=scope,
            branding=branding,
            owner_id=owner.user_id,
            owner_role=str(owner.role),
        )

    def create_schedule(self, *, definition_id: int, cadence: str, owner: Principal) -> int:
        """Attach a cadence to a definition the caller owns (task 18.4, 18.5)."""
        return self._store.create_schedule(
            definition_id=definition_id,
            cadence=cadence,
            owner_id=owner.user_id,
            owner_role=str(owner.role),
        )

    def set_schedule_active(self, schedule_id: int, *, active: bool) -> bool:
        return self._store.set_schedule_active(schedule_id, active=active)

    def get_definition(self, definition_id: int, owner: Principal) -> ReportDefinition | None:
        """A definition the caller owns, or ``None`` (ownership-scoped, task 18.5)."""
        return self._repository.get_definition(definition_id, owner.user_id)

    def list_runs(self, definition_id: int, owner: Principal) -> tuple[ReportRun, ...]:
        return self._repository.list_runs(definition_id, owner.user_id)

    def get_run(self, run_id: int, owner: Principal) -> ReportRun | None:
        return self._repository.get_run(run_id, owner.user_id)

    # ---------------------------------------------------------------- run
    def run_definition(
        self,
        definition: ReportDefinition,
        principal: Principal,
        services: Services,
        runtime: AgentRuntime | None,
        *,
        trigger_kind: str,
        deliver: bool = False,
    ) -> ReportRun:
        """Generate, persist and (optionally) deliver a report (task 18.2, 18.3, 18.4, 18.5).

        Opens a run row, generates the artifact with entitlement re-checked at this moment, writes
        it to disk, optionally delivers via the mock deliverer, and closes the run with its
        provenance.
        A generation failure is recorded as a FAILED run rather than raising, so a scheduled batch
        continues past one bad definition.
        """
        run_id = self._store.begin_run(
            definition_id=definition.definition_id,
            report_type=definition.report_type,
            triggered_by=principal.user_id,
            trigger_kind=trigger_kind,
        )
        with telemetry.report_span(str(definition.report_type)):
            try:
                artifact = generate_report(
                    GenerationRequest(
                        definition=definition,
                        principal=principal,
                        max_customers=self._max_customers,
                    ),
                    services,
                    runtime,
                )
                artifact_path = self._artifacts.write(run_id, definition, artifact)
                if deliver:
                    self._deliverer.deliver(
                        artifact_path=artifact_path, definition=definition, run_id=run_id
                    )
                self._store.finish_run(
                    run_id,
                    status=RunStatus.SUCCEEDED,
                    artifact_path=str(artifact_path),
                    customers_rendered=artifact.customers_rendered,
                    provenance=artifact.provenance,
                )
                telemetry.record_run(report_type=str(definition.report_type), outcome="succeeded")
            except Exception as exc:
                _logger.warning(
                    "report generation failed",
                    extra={
                        "definition_id": definition.definition_id,
                        "error_type": type(exc).__name__,
                    },
                )
                self._store.finish_run(
                    run_id,
                    status=RunStatus.FAILED,
                    artifact_path=None,
                    customers_rendered=0,
                    provenance=RunProvenance(),
                    error=type(exc).__name__,
                )
                telemetry.record_run(report_type=str(definition.report_type), outcome="failed")

        run = self._repository.get_run(run_id, principal.user_id)
        if run is None:  # pragma: no cover - the run was just written under this owner
            raise DatabaseFileMissingError("report run vanished after write")
        return run

    # ---------------------------------------------------------------- scheduled batch
    def run_due_schedules(
        self,
        principal_for: PrincipalResolver,
        services: Services,
        runtime: AgentRuntime | None,
        *,
        today: date | None = None,
    ) -> ScheduledRunSummary:
        """Run every schedule whose cadence is due (task 18.4).

        For each due schedule, the owner's *live* principal is resolved (never the stored snapshot)
        and the report is generated under it, so a revoked book stops receiving data. A schedule
        whose owner can no longer be resolved is skipped, not run. Delivery uses the configured
        deliverer (the offline file mock by default).
        """
        now = today or datetime.now(UTC).date()
        schedules = self._store.list_active_schedules()
        due = [s for s in schedules if self._is_due(s, now)]
        started = succeeded = failed = 0
        for schedule in due:
            principal = principal_for.resolve(schedule.owner_id)
            if principal is None:
                _logger.warning(
                    "schedule owner could not be resolved; skipping",
                    extra={"schedule_id": schedule.schedule_id},
                )
                continue
            definition = self._store.get_definition(schedule.definition_id)
            if definition is None:  # pragma: no cover - FK cascade keeps these aligned
                continue
            started += 1
            run = self.run_definition(
                definition,
                principal,
                services,
                runtime,
                trigger_kind="SCHEDULED",
                deliver=True,
            )
            if run.status is RunStatus.SUCCEEDED:
                succeeded += 1
            else:
                failed += 1
            self._store.mark_schedule_ran(schedule.schedule_id, ran_at=now_utc_date())
        return ScheduledRunSummary(
            schedules_due=len(due),
            runs_started=started,
            runs_succeeded=succeeded,
            runs_failed=failed,
        )

    @staticmethod
    def _is_due(schedule: ReportSchedule, today: date) -> bool:
        """Whether a schedule's cadence has elapsed since its last run (task 18.4)."""
        if schedule.last_run_at is None:
            return True
        try:
            last = date.fromisoformat(schedule.last_run_at[:10])
        except ValueError:  # pragma: no cover - stored value is always an ISO date
            return True
        gap = _CADENCE_DAYS.get(schedule.cadence, 1)
        return today >= last + timedelta(days=gap)


class PrincipalResolver:
    """Resolves a user id to their *live* principal at generation time (task 18.4).

    A thin holder so the scheduler never trusts the entitlement snapshot stored on a schedule: it
    asks this resolver for the owner's current principal, and a revoked or removed user resolves to
    ``None`` and is skipped. The concrete resolver is built from the identity provider on the API
    path and from the seeded local provider on the CLI path.
    """

    __slots__ = ("_resolve_fn",)

    def __init__(self, resolve_fn: Callable[[str], Principal | None]) -> None:
        self._resolve_fn = resolve_fn

    def resolve(self, user_id: str) -> Principal | None:
        return self._resolve_fn(user_id)


def build_report_service(
    reports_db_path: Path,
    *,
    output_dir: Path,
    max_customers: int,
    pool_size: int,
    deliverer: Deliverer | None = None,
) -> tuple[ReportService, Engine] | None:
    """Open ``reports.db`` read-only and construct the service, or ``None`` if not yet built.

    Best-effort, mirroring the signals service: a deployment that has not created any report yet
    still serves the whole platform, and the report endpoints report "not built" rather than
    crashing. The store (writer) and the artifact store both target the same paths. Defining the
    first report initializes the database via the store, after which the read engine can open it.
    """
    if not reports_db_path.is_file():
        # Initialize on first construction so the writer and reader agree on schema from the start.
        initialize_reports_db(reports_db_path)
    try:
        engine = create_sqlite_engine(
            reports_db_path, mode=AccessMode.READ_ONLY, pool_size=pool_size
        )
    except DatabaseFileMissingError:
        return None
    repository = ReportRepository(engine)
    store = ReportStore(reports_db_path)
    artifacts = ArtifactStore(output_dir)
    service = ReportService(
        repository,
        store,
        artifacts=artifacts,
        deliverer=deliverer or FileDeliverer(output_dir),
        max_customers=max_customers,
    )
    return service, engine


__all__ = [
    "PrincipalResolver",
    "ReportService",
    "ScheduledRunSummary",
    "build_report_service",
]
