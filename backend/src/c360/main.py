"""Application factory and ASGI entry point.

Startup order is deliberate:

1. configuration — validated first, so an invalid deployment fails before anything else runs;
2. logging — installed before telemetry so telemetry's own diagnostics are captured, redacted;
3. telemetry — the allowlist processor is in place before the first span can be created;
4. instrumentation and routes.

There is no module-level ``app``. Configuration is validated inside :func:`create_app`, and an
import-time failure inside an ASGI server is reported far less clearly than a factory call:

    uvicorn c360.main:create_app --factory
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from c360 import __version__
from c360.agents.runtime import build_agent_runtime
from c360.api.auth import AuthenticationMiddleware
from c360.api.errors import register_exception_handlers
from c360.api.middleware import (
    AccessLogMiddleware,
    CorrelationIdMiddleware,
    HttpMetricsMiddleware,
)
from c360.api.readiness import (
    REGISTRY,
    register_audit_check,
    register_config_check,
    register_customer_db_check,
    register_knowledge_check,
    register_model_check,
    register_reports_check,
)
from c360.api.routes import (
    admin,
    ask,
    auth,
    customers,
    health,
    insights,
    knowledge,
    me,
    reports,
    signals,
)
from c360.api.services import dispose_services
from c360.core.cache import TtlCache
from c360.core.config import AuthProvider, Settings, get_settings
from c360.core.context import correlation_scope
from c360.core.logging import configure_logging, get_logger
from c360.core.telemetry import (
    CostEstimator,
    instrument_app,
    setup_metrics,
    setup_telemetry,
    shutdown_telemetry,
)
from c360.domain.ports import IdentityProvider
from c360.security.audit import AuditWriter
from c360.security.provider import build_identity_provider, build_local_provider
from c360.security.session import IdleSessionTracker

_logger = get_logger(__name__)

_DESCRIPTION = """
Customer 360 Intelligence Platform API.

Deterministic customer data, agent-generated insights and grounded natural-language Q&A over a
read-only banking dataset. Monetary values are integer cents internally and decimal strings at this
boundary; every response carries a correlation ID and trace ID in `meta`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the audit writer, report advisories at startup, drain and flush at shutdown."""
    settings: Settings = app.state.settings

    audit_writer: AuditWriter = app.state.audit_writer
    audit_writer.start()

    with correlation_scope():
        for warning in settings.startup_warnings():
            _logger.warning(warning)
        # Task 7.2: verify the sqlite-vec extension loads at startup rather than on the first
        # retrieval. Non-fatal — retrieval is not required for the deterministic platform to serve,
        # and readiness reports the same fact — but a warning here turns a silent "no such module:
        # vec0" on the first knowledge query into a diagnosable startup line.
        from c360.knowledge.extension import vec_available, vec_version  # noqa: PLC0415

        if vec_available():
            _logger.info("sqlite-vec loaded", extra={"vec_version": vec_version()})
        else:
            _logger.warning(
                "sqlite-vec is not available: knowledge retrieval will be unavailable until the "
                "extension can be loaded (readiness reports this under the 'knowledge' check)."
            )
        _logger.info(
            "api started",
            extra={
                "version": __version__,
                "environment": str(settings.environment),
                "llm_provider": str(settings.llm_provider),
                "auth_provider": str(settings.auth_provider),
                "otel_enabled": settings.otel_enabled,
                "readiness_checks": list(app.state.readiness_checks),
            },
        )

    yield

    with correlation_scope():
        _logger.info("api stopping")
    dispose_services(app.state)
    # Close the Q&A conversation-memory connection (task 9.2) on the running loop before shutdown.
    agent_runtime = getattr(app.state, "agent_runtime", None)
    if agent_runtime is not None:
        await agent_runtime.memory.aclose()
    audit_writer.stop()
    shutdown_telemetry()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or get_settings()

    configure_logging(settings)
    setup_telemetry(settings)
    # The metric inventory (task 10.2). Instruments are created eagerly so the first request is
    # already measured; the cost counter is driven by the configured Bedrock price table.
    setup_metrics(CostEstimator.from_file(settings.cost_price_table))

    app = FastAPI(
        title="Customer 360 Intelligence Platform",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings

    # The identity provider (task 4.1) and the idle-session tracker (task 4.2). Under LOCAL the
    # concrete provider is built once and used *both* as the middleware's authenticator and as the
    # token endpoint's issuer — one instance, one signing key, so a token issued at /auth/token
    # verifies in the middleware. Under OIDC the middleware gets the JWKS-validating provider and
    # there is no local issuer to store.
    identity_provider: IdentityProvider
    if settings.auth_provider is AuthProvider.LOCAL:
        local_provider = build_local_provider(settings)
        identity_provider = local_provider
        app.state.local_identity_provider = local_provider
    else:
        identity_provider = build_identity_provider(settings)
    app.state.identity_provider = identity_provider
    session_tracker = IdleSessionTracker(idle_timeout_s=settings.session_idle_timeout_s)
    app.state.session_tracker = session_tracker

    # The audit sink (task 4.6). Constructed here, started by the lifespan so a bare create_app in a
    # test does not spawn a thread until a client enters the app context.
    audit_writer = AuditWriter(settings.audit_db_path)
    app.state.audit_writer = audit_writer

    # The agent runtime (Phase 8): the LLM provider, prompt registry and shared output cache. The
    # provider and prompts need no database, so the runtime is built eagerly; the dashboard graph it
    # holds is built lazily on the first insights request, when the service container exists. The
    # cache is exposed on app state so the recompute endpoint can invalidate it (design §8.7).
    agent_runtime = build_agent_runtime(settings)
    app.state.agent_runtime = agent_runtime
    app.state.agent_cache = agent_runtime.cache

    # The read-model cache (task 16.2): a short-TTL tier for deterministic reads, behind the shared
    # Cache interface so a Redis backend swaps in without a call-site change. Exposed on app state
    # so the recompute path invalidates it alongside the agent cache. A zero TTL disables it.
    app.state.read_model_cache = TtlCache[object](ttl_s=settings.read_model_cache_ttl_s)

    # Added last runs first. The order that produces on the wire is:
    #   CorrelationId -> Authentication -> AccessLog -> handler
    # so the correlation ID is bound before authentication can emit a 401 (that 401 must carry an
    # ID), and the access log line is written for authenticated and rejected requests alike, inside
    # both the correlation scope and the instrumentation's server span.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(HttpMetricsMiddleware, budget_ms=settings.http_request_budget_ms)
    app.add_middleware(
        AuthenticationMiddleware,
        identity_provider=identity_provider,
        session_tracker=session_tracker,
    )
    app.add_middleware(CorrelationIdMiddleware)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(me.router)
    app.include_router(customers.router)
    app.include_router(knowledge.router)
    app.include_router(insights.router)
    app.include_router(ask.router)
    app.include_router(signals.router)
    app.include_router(reports.router)

    register_config_check(settings)
    register_audit_check(audit_writer)
    register_knowledge_check(settings)
    register_customer_db_check(settings)
    register_model_check(agent_runtime)
    register_reports_check(settings)
    app.state.readiness_checks = REGISTRY.names

    instrument_app(app, settings)
    return app
