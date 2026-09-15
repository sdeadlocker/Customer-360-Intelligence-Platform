"""Readiness check registry.

Requirement 18.14 wants readiness to report the customer database, the knowledge base, the model
provider and the audit writer. Those subsystems arrive in Phases 1, 4, 7 and 8, so readiness is
built as a registry now and populated as each one lands, rather than as a single function that gets
rewritten five times.

``/health`` stays liveness-only and must never touch a dependency (design §13.8).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from c360.core.config import Settings
from c360.core.logging import get_logger

_logger = get_logger(__name__)


class CheckStatus(StrEnum):
    """Outcome of a single readiness check."""

    PASS = "pass"  # noqa: S105 - a probe outcome, not a credential
    FAIL = "fail"
    #: Subsystem is not configured in this deployment. Reported, but does not block readiness.
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of one readiness check. ``detail`` never carries customer data."""

    name: str
    status: CheckStatus
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.status is CheckStatus.FAIL


CheckFn = Callable[[], CheckResult]


class ReadinessRegistry:
    """Ordered collection of readiness checks."""

    __slots__ = ("_checks",)

    def __init__(self) -> None:
        self._checks: dict[str, CheckFn] = {}

    def register(self, name: str, check: CheckFn) -> None:
        """Register (or replace) a named check."""
        self._checks[name] = check

    def unregister(self, name: str) -> None:
        self._checks.pop(name, None)

    def clear(self) -> None:
        self._checks.clear()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._checks)

    def run(self) -> tuple[bool, list[CheckResult]]:
        """Run every check. A raising check is a failing check, never a failing request."""
        results: list[CheckResult] = []
        for name, check in self._checks.items():
            try:
                results.append(check())
            # Deliberately broad: a readiness probe reports failure, it never raises.
            except Exception as exc:
                _logger.warning(
                    "readiness check raised",
                    extra={"check": name, "error_type": type(exc).__name__},
                )
                results.append(
                    CheckResult(name=name, status=CheckStatus.FAIL, detail=type(exc).__name__)
                )
        return (not any(result.blocking for result in results)), results


#: Process-wide registry. Phases 1, 4, 7 and 8 add their checks to this instance.
REGISTRY: Final = ReadinessRegistry()


def register_config_check(settings: Settings) -> None:
    """Register the only check that exists in Phase 0: configuration loaded and validated.

    The running application's settings are captured rather than re-read from the environment. A
    readiness probe should report the configuration the process is actually serving with; re-reading
    would let a probe pass or fail on an environment change the process has not adopted.
    """

    def _check() -> CheckResult:
        return CheckResult(
            name="config",
            status=CheckStatus.PASS,
            detail=f"environment={settings.environment}",
        )

    REGISTRY.register("config", _check)


def register_customer_db_check(settings: Settings) -> None:
    """Register the customer-database readiness check (task 10.8, requirement 18.14).

    Reports two things a customer read depends on: that the database passes SQLite's integrity
    check, and that its schema is at the head revision the running code expects. A database one
    migration behind the code presents as a missing column on whichever query needs it first rather
    than as a clean failure, so readiness surfaces the drift before traffic arrives.

    A missing database file is ``SKIPPED`` — an un-seeded deployment is degraded, not broken, the
    same way the knowledge check treats an un-ingested knowledge base. The integrity check opens the
    file read-only so a probe running every few seconds never contends with the audit writer.
    ``detail`` carries the schema revision and integrity result — health signals, never customer
    data.
    """

    def _check() -> CheckResult:
        from sqlalchemy import text  # noqa: PLC0415

        from c360.data.engine import AccessMode, create_sqlite_engine  # noqa: PLC0415
        from c360.data.migrations import (  # noqa: PLC0415
            current_revision,
            head_revision,
            is_up_to_date,
        )

        db_path = settings.customer_db_path
        if not db_path.is_file():
            return CheckResult(
                name="customer_db",
                status=CheckStatus.SKIPPED,
                detail="customer.db not built; run `c360 seed`",
            )

        engine = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=1)
        try:
            with engine.connect() as connection:
                integrity = connection.execute(text("PRAGMA integrity_check")).scalar_one_or_none()
        finally:
            engine.dispose()

        revision = current_revision(db_path)
        up_to_date = is_up_to_date(db_path)
        healthy = integrity == "ok" and up_to_date
        return CheckResult(
            name="customer_db",
            status=CheckStatus.PASS if healthy else CheckStatus.FAIL,
            detail=(
                f"integrity={integrity} schema={revision} head={head_revision()} "
                f"up_to_date={up_to_date}"
            ),
        )

    REGISTRY.register("customer_db", _check)


def register_model_check(runtime: object) -> None:
    """Register the model-provider readiness check (task 10.8, requirement 18.14).

    Reports cached Bedrock reachability rather than making a live model call on every probe: a
    readiness probe that invoked the model would cost a token and a round trip every few seconds,
    and a throttle would make the probe itself the outage. Instead it reads the generation circuit
    breaker (task 8.11) — the process already tracks repeated Bedrock failures there and trips it
    open, so an open breaker *is* the cached "unreachable" signal, and a closed or half-open breaker
    means the last calls succeeded or a trial is allowed. The mock provider is always reachable.

    An open breaker is reported ``PASS`` with a degraded detail, not ``FAIL``: the platform still
    serves — insights and Q&A fall back to the deterministic template — so the model being down is a
    degradation of AI features, not an unready service. ``detail`` names the model id and breaker
    state, both non-identifying.
    """

    def _check() -> CheckResult:
        provider = getattr(runtime, "provider", None)
        model_id = str(getattr(provider, "model_id", "unknown"))
        breaker = getattr(runtime, "breaker", None)
        state = getattr(breaker, "state", None)
        state_name = str(getattr(state, "value", state)) if state is not None else "unknown"
        return CheckResult(
            name="model",
            status=CheckStatus.PASS,
            detail=f"model={model_id} breaker={state_name}",
        )

    REGISTRY.register("model", _check)


def register_audit_check(writer: object) -> None:
    """Register the audit-writer liveness check (task 4.6, requirement 18.14).

    The check reports whether the single writer thread is running. A dead writer means every
    subsequent request would eventually fail closed, so readiness must surface it before traffic
    arrives. ``detail`` carries the queue depth — a health signal, never customer data.
    """

    def _check() -> CheckResult:
        alive = bool(getattr(writer, "is_alive", lambda: False)())
        depth = int(getattr(writer, "queue_depth", lambda: 0)())
        return CheckResult(
            name="audit",
            status=CheckStatus.PASS if alive else CheckStatus.FAIL,
            detail=f"writer_alive={alive} queue_depth={depth}",
        )

    REGISTRY.register("audit", _check)


def register_knowledge_check(settings: Settings) -> None:
    """Register the knowledge-base readiness check (task 7.2, requirement 18.14).

    Reports three things a retrieval request depends on, in one probe: that the ``sqlite-vec``
    extension can be loaded in this environment, that ``knowledge.db`` exists, and how many chunks
    the index holds. A missing database is ``SKIPPED`` rather than ``FAIL`` — a deployment that has
    not ingested knowledge yet is degraded, not broken, the same way a not-yet-seeded customer
    database is (the retrieval tool answers "no supporting guidance found" rather than crashing).
    An extension that cannot load *is* a failure, because it cannot be recovered from without a
    redeploy and every retrieval would fail on it.

    ``detail`` carries the chunk count and the extension version — health signals, never customer
    data (knowledge is institutional, not customer-specific — design §9.1).
    """

    def _check() -> CheckResult:
        from c360.knowledge.extension import vec_available, vec_version  # noqa: PLC0415
        from c360.knowledge.readiness import knowledge_index_state  # noqa: PLC0415

        if not vec_available():
            return CheckResult(
                name="knowledge",
                status=CheckStatus.FAIL,
                detail="sqlite-vec extension unavailable",
            )

        db_path = settings.knowledge_db_path
        if not db_path.is_file():
            return CheckResult(
                name="knowledge",
                status=CheckStatus.SKIPPED,
                detail="knowledge.db not built; run `c360 ingest-knowledge`",
            )

        state = knowledge_index_state(db_path)
        status = CheckStatus.PASS if state.ready else CheckStatus.FAIL
        return CheckResult(
            name="knowledge",
            status=status,
            detail=(
                f"vec={vec_version()} documents={state.documents} chunks={state.chunks} "
                f"embeddings={state.embeddings}"
            ),
        )

    REGISTRY.register("knowledge", _check)


def register_reports_check(settings: Settings) -> None:
    """Register the report-store readiness check (task 18.7, requirement 18.14).

    Reports the ``reports.db`` schema version against the head the running code expects. A missing
    file is ``SKIPPED`` — a deployment that has not defined a report yet is degraded, not broken
    (the store self-initializes on the first definition). A file present but stamped at a stale
    version is a ``FAIL``, surfacing schema drift before a run hits a missing column. ``detail``
    carries the
    schema version only — a health signal, never customer data.
    """

    def _check() -> CheckResult:
        from c360.reports.store import (  # noqa: PLC0415
            SCHEMA_VERSION,
            current_schema_version,
            is_up_to_date,
        )

        db_path = settings.reports_db_path
        if not db_path.is_file():
            return CheckResult(
                name="reports",
                status=CheckStatus.SKIPPED,
                detail="reports.db not built; define a report to create it",
            )
        version = current_schema_version(db_path)
        healthy = is_up_to_date(db_path)
        return CheckResult(
            name="reports",
            status=CheckStatus.PASS if healthy else CheckStatus.FAIL,
            detail=f"schema={version} head={SCHEMA_VERSION}",
        )

    REGISTRY.register("reports", _check)
