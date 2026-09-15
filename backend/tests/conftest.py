"""Shared fixtures.

Two properties are enforced here rather than in each test:

* **Hermetic configuration.** ``Settings`` reads ``<repo>/.env`` in normal operation. Every test
  builds settings with ``_env_file=None`` so a developer's local ``.env`` can never change a test
  outcome.
* **Isolated telemetry.** The SDK's global providers are process-wide and their setters are
  one-shot by design, so the globals are cleared between tests. Nothing outside this module
  touches them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from c360.api.readiness import REGISTRY
from c360.core.config import Settings
from c360.core.logging import configure_logging, reset_logging
from c360.core.telemetry import bootstrap
from c360.core.telemetry.processor import AllowlistSpanProcessor
from c360.main import create_app

# Configuration that is valid, complete and contains nothing real.
BASE_ENV: dict[str, str] = {
    "ENVIRONMENT": "local",
    "LLM_PROVIDER": "mock",
    "AUTH_PROVIDER": "local",
    "OTEL_ENABLED": "true",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_SERVICE_NAME": "c360-api-test",
    "TELEMETRY_CUSTOMER_HASH_SALT": "test-salt-not-a-secret",
    "LOG_FORMAT": "json",
}


def make_settings(**overrides: Any) -> Settings:
    """Build settings from :data:`BASE_ENV` plus overrides, ignoring any local ``.env``.

    ``BaseSettings.__init__`` is typed with its own keyword arguments, so the call is made through
    a cast rather than littered with per-argument ignores.
    """
    values = {**BASE_ENV, **{key.upper(): str(value) for key, value in overrides.items()}}
    build = cast("Callable[..., Settings]", Settings)
    return build(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    # Point the audit database at a throwaway location so a test never writes into the repo's
    # data/ directory and tests never contend over one audit.db (task 4.6).
    audit_dir = tmp_path_factory.mktemp("audit")
    return make_settings(sqlite_audit_db_path=str(audit_dir / "audit.db"))


@pytest.fixture(autouse=True)
def _reset_global_state() -> Iterator[None]:
    """Reset logging, telemetry and readiness state around every test."""
    yield
    reset_logging()
    bootstrap._state.value = None

    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    metrics_internal._METER_PROVIDER = None
    metrics_internal._METER_PROVIDER_SET_ONCE._done = False

    REGISTRY.clear()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


@pytest.fixture
def span_exporter() -> Iterator[InMemorySpanExporter]:
    """Install a tracer provider that captures allowlist-filtered spans in memory.

    The allowlist processor sits in front of the exporter exactly as it does in production, so
    assertions about exported attributes are assertions about what would really be exported.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(AllowlistSpanProcessor(SimpleSpanProcessor(exporter)))

    previous = trace.get_tracer_provider()
    trace._TRACER_PROVIDER = provider
    try:
        yield exporter
    finally:
        provider.shutdown()
        trace._TRACER_PROVIDER = previous


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    REGISTRY.clear()
    configure_logging(settings, force=True)
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- Phase 5 dataset fixtures
# These build a generated, recomputed customer database (the dataset the Phase 5 gate names) and are
# defined here rather than imported into each test module so pytest discovers them by name — an
# import-and-reuse would trip ruff's redefinition check on every use. Session-scoped so the generate
# + recompute cost is paid once; only tests that request them pay anything at all.
from pathlib import Path as _Path  # noqa: E402

from c360.data.engine import AccessMode as _AccessMode  # noqa: E402
from c360.data.engine import create_sqlite_engine as _create_engine  # noqa: E402
from c360.data.recompute import recompute as _recompute  # noqa: E402
from c360.generator.pipeline import seed as _seed  # noqa: E402

PHASE5_PANEL_COUNT = 40
PHASE5_PANEL_SEED = 42


@pytest.fixture(scope="session")
def phase5_db(tmp_path_factory: pytest.TempPathFactory) -> _Path:
    """A generated, recomputed customer database, built once for the whole test session."""
    path = tmp_path_factory.mktemp("phase5") / "customer.db"
    _seed(database=path, count=PHASE5_PANEL_COUNT, seed=PHASE5_PANEL_SEED)
    _recompute(path)
    return path


@pytest.fixture(scope="session")
def phase5_engine(phase5_db: _Path) -> Iterator[Any]:
    engine = _create_engine(phase5_db, mode=_AccessMode.READ_ONLY, pool_size=4)
    try:
        yield engine
    finally:
        engine.dispose()
