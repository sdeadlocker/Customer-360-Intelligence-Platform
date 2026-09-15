"""Read-only access to ``reports.db`` for the report API (task 18.5).

The API opens ``reports.db`` ``mode=ro`` — the same read-only rule the customer, knowledge and
signals databases follow — so a read path can never take a write lock or contend with a generation
run. Writes (define, schedule, run lifecycle) go through :class:`~c360.reports.store.ReportStore`,
never here.

Ownership scoping is applied **in SQL**: a caller sees only the definitions and runs they own, so a
report's run history never reveals another owner's activity. The customer/book entitlement that
governs a report's *content* is re-checked at generation time (task 18.4), independently of this
ownership filter.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from c360.reports.models import (
    Branding,
    ReportDefinition,
    ReportRun,
    ReportScope,
    ReportType,
    RunProvenance,
    RunStatus,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine


def _row_to_definition(row: Any) -> ReportDefinition:
    return ReportDefinition(
        definition_id=int(row.definition_id),
        report_type=ReportType(row.report_type),
        title=str(row.title),
        scope=ReportScope.from_dict(json.loads(row.scope)),
        branding=Branding.from_dict(json.loads(row.branding)),
        owner_id=str(row.owner_id),
        owner_role=str(row.owner_role),
        created_at=str(row.created_at),
    )


def _row_to_run(row: Any) -> ReportRun:
    return ReportRun(
        run_id=int(row.run_id),
        definition_id=int(row.definition_id),
        report_type=ReportType(row.report_type),
        status=RunStatus(row.status),
        triggered_by=str(row.triggered_by),
        trigger_kind=str(row.trigger_kind),
        artifact_path=str(row.artifact_path) if row.artifact_path is not None else None,
        customers_rendered=int(row.customers_rendered),
        provenance=RunProvenance.from_dict(json.loads(row.provenance)),
        started_at=str(row.started_at),
        finished_at=str(row.finished_at) if row.finished_at is not None else None,
        error=str(row.error) if row.error is not None else None,
    )


class ReportRepository:
    """Read-only reads of ``reports.db``, scoped to the calling owner."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_definition(self, definition_id: int, owner_id: str) -> ReportDefinition | None:
        """A definition the caller owns, or ``None`` (also ``None`` for another owner's id)."""
        sql = """
            SELECT * FROM report_definition
            WHERE definition_id = :did AND owner_id = :owner
        """
        with self._engine.connect() as connection:
            row = connection.execute(
                text(sql), {"did": definition_id, "owner": owner_id}
            ).fetchone()
        return _row_to_definition(row) if row is not None else None

    def list_runs(self, definition_id: int, owner_id: str) -> tuple[ReportRun, ...]:
        """Every run of a definition the caller owns, newest first (task 18.5).

        Joined to ``report_definition`` on ``owner_id`` so a run for another owner's definition is
        never returned — the ownership gate is a query property, not a post-filter.
        """
        sql = """
            SELECT r.* FROM report_run r
            JOIN report_definition d ON d.definition_id = r.definition_id
            WHERE r.definition_id = :did AND d.owner_id = :owner
            ORDER BY r.run_id DESC
        """
        with self._engine.connect() as connection:
            rows = list(
                connection.execute(text(sql), {"did": definition_id, "owner": owner_id}).all()
            )
        return tuple(_row_to_run(row) for row in rows)

    def get_run(self, run_id: int, owner_id: str) -> ReportRun | None:
        """One run the caller owns, or ``None`` (also ``None`` for another owner's run)."""
        sql = """
            SELECT r.* FROM report_run r
            JOIN report_definition d ON d.definition_id = r.definition_id
            WHERE r.run_id = :rid AND d.owner_id = :owner
        """
        with self._engine.connect() as connection:
            row = connection.execute(text(sql), {"rid": run_id, "owner": owner_id}).fetchone()
        return _row_to_run(row) if row is not None else None


__all__ = ["ReportRepository"]
