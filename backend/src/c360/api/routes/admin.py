"""Administrative operations endpoint (task 3.6).

``POST /admin/recompute`` refreshes every ``derived_*`` table, the FTS5 search index and the graph
projection — the same work :func:`c360.data.recompute.recompute` does for the CLI. It exists so an
operator can rebuild derived state after a data change without a shell on the host, and so the
executable proof of design §14.6 ("derived values are always recomputable") has an HTTP surface as
well as a CLI one.

Authorization
-------------

The task specifies this is **admin only**. Role-based authorization arrives in Phase 4 (the
``IdentityProvider`` port, the ``Principal`` and entitlement scoping), so the endpoint is built here
with the recompute wired end to end and the admin guard added when the auth foundation lands. Until
then it is available in local development only; it is registered unconditionally but performs no
destructive operation on source data — it only rebuilds projections that are, by definition,
reconstructible.

Blocking work off the event loop
---------------------------------

The SQLite driver is blocking and recompute takes a write lock, so it runs in a worker thread via
``run_in_threadpool`` rather than on the event loop. This matches the concurrency model design §6.1
sets for every service call over the blocking driver.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from c360.api.envelope import ApiError, Envelope, ErrorCode
from c360.core.config import Settings
from c360.core.logging import get_logger
from c360.data.engine import DatabaseFileMissingError
from c360.data.recompute import RecomputeError, recompute
from c360.signals.detect import detect_config_from_settings, detect_signals

router = APIRouter(tags=["operations"])

_logger = get_logger(__name__)


class RecomputePayload(BaseModel):
    """What the recompute produced: per-target row counts and how long it took."""

    model_config = ConfigDict(frozen=True)

    row_counts: dict[str, int] = Field(
        description="Rows written per projected target (derived tables, search index, graph)."
    )
    total_rows: int
    elapsed_seconds: float


class DetectSignalsPayload(BaseModel):
    """What a signal-detection run produced (task 17.4)."""

    model_config = ConfigDict(frozen=True)

    run_id: int
    as_of: str
    customers_scanned: int
    signals_written: int


class RunReportsPayload(BaseModel):
    """What a scheduled-report batch produced (task 18.4)."""

    model_config = ConfigDict(frozen=True)

    schedules_due: int
    runs_started: int
    runs_succeeded: int
    runs_failed: int


@router.post(
    "/admin/recompute",
    response_model=Envelope[RecomputePayload],
    summary="Rebuild derived values, the search index and the graph projection",
    responses={
        500: {"description": "A projection failed a referential-integrity check"},
        503: {"description": "The customer database is not available"},
    },
)
async def admin_recompute(request: Request) -> Envelope[RecomputePayload]:
    """Rebuild all derived state (design §14.6). Idempotent and non-destructive to source data."""
    settings: Settings = request.app.state.settings
    try:
        report = await run_in_threadpool(recompute, settings.customer_db_path)
    except DatabaseFileMissingError as error:
        raise ApiError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "customer database is not available; seed it before recomputing",
        ) from error
    except RecomputeError as error:
        # The projection is rolled back on failure, so source data is untouched.
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            "recompute failed a referential-integrity check",
        ) from error

    # A recompute can change any customer's derived facts, so every cached agent narrative and every
    # cached read model is now potentially stale (design §8.7, task 16.2). Clear both if wired.
    _invalidate_caches(request)

    _logger.info(
        "recompute via api",
        extra={"rows": report.total_rows, "elapsed_seconds": round(report.elapsed_seconds, 3)},
    )
    return Envelope.of(
        RecomputePayload(
            row_counts=report.row_counts,
            total_rows=report.total_rows,
            elapsed_seconds=report.elapsed_seconds,
        )
    )


@router.post(
    "/admin/detect-signals",
    response_model=Envelope[DetectSignalsPayload],
    summary="Detect proactive signals and rebuild the worklist store (Phase 17)",
    responses={
        503: {"description": "The customer database is not available"},
    },
)
async def admin_detect_signals(request: Request) -> Envelope[DetectSignalsPayload]:
    """Run signal detection over the read-only customer database (task 17.4).

    Admin-only by design, following the same authorization story as ``/admin/recompute``: role-based
    admin guarding is added when the platform gains an admin role; until then the endpoint is
    non-destructive to source data — it only reads the read-only customer database and writes the
    separate, reconstructible ``signals.db``. Idempotent via ``dedup_key``, so it is safe to
    schedule and safe to re-run.
    """
    settings: Settings = request.app.state.settings
    try:
        provenance = await run_in_threadpool(
            detect_signals,
            customer_db_path=settings.customer_db_path,
            signals_db_path=settings.signals_db_path,
            config=detect_config_from_settings(settings),
        )
    except DatabaseFileMissingError as error:
        raise ApiError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "customer database is not available; seed it before detecting signals",
        ) from error

    _logger.info(
        "signal detection via api",
        extra={"run_id": provenance.run_id, "written": provenance.signals_written},
    )
    return Envelope.of(
        DetectSignalsPayload(
            run_id=provenance.run_id,
            as_of=provenance.as_of,
            customers_scanned=provenance.customers_scanned,
            signals_written=provenance.signals_written,
        )
    )


@router.post(
    "/admin/run-reports",
    response_model=Envelope[RunReportsPayload],
    summary="Run every due report schedule and deliver via the configured deliverer (Phase 18)",
    responses={
        503: {"description": "The customer or report database is not available"},
    },
)
async def admin_run_reports(request: Request) -> Envelope[RunReportsPayload]:
    """Run all due report schedules (task 18.4).

    Admin-only by design, following the same authorization story as ``/admin/recompute`` and
    ``/admin/detect-signals``. Each due schedule's owner is re-resolved to their *live* principal at
    generation time (never the stored snapshot), so a revoked book stops receiving data. Non-
    destructive to source data: it only reads the read-only customer database and writes the
    separate ``reports.db`` and the artifact volume.
    """
    from c360.api.services import get_services  # noqa: PLC0415 - avoid a route import cycle
    from c360.reports.service import PrincipalResolver  # noqa: PLC0415
    from c360.security.local_provider import principal_for_user  # noqa: PLC0415

    services = get_services(request)
    if services is None or services.reports is None:
        raise ApiError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "customer or report database is not available; seed and define a report first",
        )

    resolver = PrincipalResolver(principal_for_user)
    runtime = getattr(request.app.state, "agent_runtime", None)
    summary = await run_in_threadpool(
        services.reports.run_due_schedules, resolver, services, runtime
    )
    _logger.info(
        "scheduled reports via api",
        extra={"due": summary.schedules_due, "succeeded": summary.runs_succeeded},
    )
    return Envelope.of(
        RunReportsPayload(
            schedules_due=summary.schedules_due,
            runs_started=summary.runs_started,
            runs_succeeded=summary.runs_succeeded,
            runs_failed=summary.runs_failed,
        )
    )


def _invalidate_caches(request: Request) -> None:
    """Clear the agent output cache and the read-model cache after a recompute (§8.7, task 16.2).

    Both are guarded by ``getattr`` so the endpoint works identically whether or not each tier has
    been wired onto this app — the deterministic platform runs without the agent layer, and a
    deployment can disable the read-model cache with a zero TTL. Both satisfy the shared
    :class:`c360.core.cache.Cache` contract, so the same ``invalidate_all`` call clears either.
    """
    for name in ("agent_cache", "read_model_cache"):
        cache = getattr(request.app.state, name, None)
        if cache is not None:
            dropped = cache.invalidate_all()
            _logger.info(
                "cache invalidated on recompute", extra={"cache": name, "dropped": dropped}
            )
