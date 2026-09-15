"""Readiness checks added in Phase 10 (task 10.8): customer-DB integrity/schema and model provider.

These probe the two subsystems requirement 18.14 names that had no check before Phase 10. The
checks are registered on the process-wide REGISTRY, so each test registers, asserts, and cleans up.
"""

from __future__ import annotations

from pathlib import Path

from c360.api.readiness import (
    REGISTRY,
    CheckResult,
    CheckStatus,
    register_customer_db_check,
    register_model_check,
)
from c360.data.migrations import downgrade_database, head_revision, upgrade_database
from tests.conftest import make_settings


def _run(name: str) -> CheckResult:
    _, results = REGISTRY.run()
    return next(result for result in results if result.name == name)


class TestCustomerDbCheck:
    def test_missing_database_is_skipped(self, tmp_path: Path) -> None:
        settings = make_settings(sqlite_db_path=str(tmp_path / "absent.db"))
        register_customer_db_check(settings)
        try:
            result = _run("customer_db")
            assert result.status is CheckStatus.SKIPPED
        finally:
            REGISTRY.unregister("customer_db")

    def test_migrated_database_passes_with_schema_detail(self, tmp_path: Path) -> None:
        db_path = tmp_path / "customer.db"
        upgrade_database(db_path)
        settings = make_settings(sqlite_db_path=str(db_path))
        register_customer_db_check(settings)
        try:
            result = _run("customer_db")
            assert result.status is CheckStatus.PASS
            assert "integrity=ok" in result.detail
            assert head_revision() in result.detail
        finally:
            REGISTRY.unregister("customer_db")

    def test_schema_drift_fails(self, tmp_path: Path) -> None:
        # A database behind the head revision is a deployment error readiness must surface.
        db_path = tmp_path / "customer.db"
        upgrade_database(db_path)
        downgrade_database(db_path, "-1")
        settings = make_settings(sqlite_db_path=str(db_path))
        register_customer_db_check(settings)
        try:
            result = _run("customer_db")
            assert result.status is CheckStatus.FAIL
            assert "up_to_date=False" in result.detail
        finally:
            REGISTRY.unregister("customer_db")


class _Runtime:
    def __init__(self, provider: object, breaker: object) -> None:
        self.provider = provider
        self.breaker = breaker


class _Provider:
    model_id = "mock-model"


class _Breaker:
    def __init__(self, state: str) -> None:
        self._state = state

    @property
    def state(self) -> str:
        return self._state


class TestModelCheck:
    def test_reports_model_and_breaker_state(self) -> None:
        runtime = _Runtime(_Provider(), _Breaker("closed"))
        register_model_check(runtime)
        try:
            result = _run("model")
            assert result.status is CheckStatus.PASS
            assert "model=mock-model" in result.detail
            assert "breaker=closed" in result.detail
        finally:
            REGISTRY.unregister("model")

    def test_open_breaker_is_still_pass_but_degraded(self) -> None:
        # An open breaker means AI features are degraded to templates — the service is still ready.
        runtime = _Runtime(_Provider(), _Breaker("open"))
        register_model_check(runtime)
        try:
            result = _run("model")
            assert result.status is CheckStatus.PASS
            assert "breaker=open" in result.detail
        finally:
            REGISTRY.unregister("model")
