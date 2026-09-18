"""Service wiring and FastAPI dependencies for the customer API (task 5.7, 5.8).

The customer database is read-only and opened ``mode=ro``, so opening it fails outright when the
file
does not exist (design §12.3). That is correct at runtime but wrong for app construction: a test —
and
a fresh checkout before ``c360 seed`` has run — builds the app to exercise ``/health`` and ``/auth``
without a database. So the engine and the service container are built *lazily*, on first use, rather
than in :func:`create_app`. The container is cached on ``app.state`` once built.

A route reaches the services through :func:`require_services`, which returns the container or raises
``UPSTREAM_UNAVAILABLE`` (503) when the database is absent — the same fail-soft shape the readiness
check reports, so a not-yet-seeded deployment answers "not ready" rather than crashing on every
read.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from c360.api.envelope import ApiError, ErrorCode
from c360.core.logging import get_logger
from c360.data.engine import AccessMode, DatabaseFileMissingError, create_sqlite_engine
from c360.data.repositories import build_repositories
from c360.services.aggregator import C360Aggregator
from c360.services.customer import CustomerService, RecentlyViewedTracker
from c360.services.fee_recovery import FeeRecoveryService
from c360.services.financial import FinancialService
from c360.services.journey import JourneyService
from c360.services.knowledge import KnowledgeService
from c360.services.offer import OfferService
from c360.services.relationship import RelationshipService
from c360.services.risk import RiskService
from c360.signals.service import SignalService, build_signal_service

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from c360.core.config import Settings
    from c360.data.repositories.fee_recovery import SqliteFeeRecoveryRepository
    from c360.reports.service import ReportService

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Services:
    """The service container mounted on ``app.state`` and shared across requests.

    One engine, one repository bundle, one instance of each service — services hold no per-request
    state, so sharing them is safe and keeps the connection pool single and bounded (design §12.3).
    The aggregator composes the six domain services; the recently-viewed tracker is shared so a
    user's list is consistent across requests in the process.
    """

    engine: Engine
    customer: CustomerService
    financial: FinancialService
    relationship: RelationshipService
    risk: RiskService
    offer: OfferService
    journey: JourneyService
    aggregator: C360Aggregator
    #: The optional subsystems default to ``None`` so a focused test can construct the container
    #: with just the core services; :func:`build_services` always passes each one explicitly.
    #: Knowledge retrieval (Phase 7). ``None`` when ``knowledge.db`` has not been ingested yet or
    #: sqlite-vec is unavailable — the deterministic platform serves without it, and the knowledge
    #: endpoints/tool report "not available" rather than crashing (mirrors the not-yet-seeded
    #: customer-database story).
    knowledge: KnowledgeService | None = None
    #: The knowledge engine, held so it can be disposed at shutdown. ``None`` when knowledge is off.
    knowledge_engine: Engine | None = None
    #: The proactive-signals worklist (Phase 17). ``None`` until ``c360 detect-signals`` has built
    #: ``signals.db`` — the worklist endpoints then report an empty feed rather than crashing, the
    #: same not-yet-built story as knowledge.
    signals: SignalService | None = None
    #: The signals engine, held so it can be disposed at shutdown. ``None`` when signals are off.
    signals_engine: Engine | None = None
    #: The report-exports service (Phase 18). Always present once the customer database is built —
    #: unlike knowledge/signals it creates its own writable ``reports.db`` on first construction, so
    #: the report endpoints are available the moment the platform serves.
    reports: ReportService | None = None
    #: The reports read engine, disposed at shutdown. ``None`` when reports are off.
    reports_engine: Engine | None = None
    #: Fee recovery (Phase 22 play 6). Reads the same read-only customer engine as every other
    #: deterministic service, so it owns no engine of its own and needs no disposal. ``None`` only
    #: when the fee schedule could not be loaded — a deployment fault the endpoint reports as 503
    #: rather than answering "no leakage", which would read as good news.
    fee_recovery: FeeRecoveryService | None = None


def build_services(settings: Settings) -> Services:
    """Open the customer database and construct every service over it.

    Raises:
        DatabaseFileMissingError: the customer database has not been created yet.
    """
    engine = create_sqlite_engine(
        settings.customer_db_path,
        mode=AccessMode.READ_ONLY,
        pool_size=settings.sqlite_read_pool_size,
    )
    repositories = build_repositories(engine)
    recently_viewed = RecentlyViewedTracker()

    customer = CustomerService(repositories.customer, recently_viewed=recently_viewed)
    financial = FinancialService(
        repositories.financial, spend_anomaly_sigma=settings.spend_anomaly_sigma
    )
    relationship = RelationshipService(
        repositories.relationship,
        repositories.graph,
        repositories.customer,
        repositories.financial,
        max_hops=settings.graph_max_hops,
        node_cap=settings.graph_node_cap,
    )
    risk = RiskService(repositories.risk)
    offer = OfferService(repositories.offer, cooling_off_days=settings.offer_cooling_off_days)
    journey = JourneyService(
        repositories.journey,
        repositories.relationship,
        major_txn_absolute_threshold_cents=settings.major_txn_absolute_threshold_cents,
        major_txn_median_multiple=settings.major_txn_median_multiple,
    )
    aggregator = C360Aggregator(
        customer=customer,
        financial=financial,
        relationship=relationship,
        risk=risk,
        offer=offer,
        journey=journey,
        max_workers=settings.sqlite_read_pool_size,
    )
    knowledge, knowledge_engine = _build_knowledge(settings)
    signals, signals_engine = _build_signals(settings)
    reports, reports_engine = _build_reports(settings)
    fee_recovery = _build_fee_recovery(settings, repositories.fee_recovery)
    return Services(
        engine=engine,
        customer=customer,
        financial=financial,
        relationship=relationship,
        risk=risk,
        offer=offer,
        journey=journey,
        aggregator=aggregator,
        knowledge=knowledge,
        knowledge_engine=knowledge_engine,
        signals=signals,
        signals_engine=signals_engine,
        reports=reports,
        reports_engine=reports_engine,
        fee_recovery=fee_recovery,
    )


def _build_fee_recovery(
    settings: Settings, repository: SqliteFeeRecoveryRepository
) -> FeeRecoveryService | None:
    """Construct the fee-recovery scan, or ``None`` if the fee schedule cannot be read.

    The schedule is a committed artifact (``config/fee_schedule.json``), so a failure here means a
    deployment did not ship it — a real fault, and one that must not be papered over. Returning
    ``None`` makes the endpoint answer 503 with a clear message; the alternative, an empty schedule,
    would make the scan report no findings, and "no leakage" is indistinguishable from "working".
    """
    from c360.domain.fees import FeeSchedule, FeeScheduleError  # noqa: PLC0415 - avoid import cycle

    try:
        schedule = FeeSchedule.from_file(settings.fee_schedule)
    except FeeScheduleError as error:
        _logger.error(
            "fee schedule unavailable; fee recovery will report 503",
            extra={"path": str(settings.fee_schedule), "error": str(error)},
        )
        return None
    return FeeRecoveryService(
        repository,
        schedule,
        courtesy_waiver_cycles=settings.fee_recovery_courtesy_waiver_cycles,
        min_finding_cents=settings.fee_recovery_min_finding_cents,
        lookback_months=settings.fee_recovery_lookback_months,
    )


def _build_reports(settings: Settings) -> tuple[ReportService | None, Engine | None]:
    """Construct the report-exports service, creating ``reports.db`` on first use (Phase 18).

    Unlike knowledge/signals, the report store is *writable* and self-initializing: the builder
    creates ``reports.db`` at its own schema head the first time it runs, so the report endpoints
    are available immediately rather than after a separate build step. The artifact output directory
    on
    the ``data/`` volume is created lazily when the first artifact is written.
    """
    from c360.reports.service import build_report_service  # noqa: PLC0415 - avoid import cycle

    built = build_report_service(
        settings.reports_db_path,
        output_dir=settings.reports_output_path,
        max_customers=settings.reports_max_customers_per_run,
        pool_size=settings.sqlite_read_pool_size,
    )
    if built is None:
        return None, None
    service, reports_engine = built
    return service, reports_engine


def _build_signals(settings: Settings) -> tuple[SignalService | None, Engine | None]:
    """Open ``signals.db`` read-only and construct the worklist service, or ``(None, None)``.

    Best-effort, exactly like knowledge: a deployment that has not run ``c360 detect-signals`` yet
    still serves the whole platform; the worklist endpoints then report an empty feed. The write
    path (dismiss/ack) targets the same file through the service's store.
    """
    built = build_signal_service(
        settings.signals_db_path,
        cooling_off_days=settings.signal_cooling_off_days,
        pool_size=settings.sqlite_read_pool_size,
    )
    if built is None:
        return None, None
    service, signals_engine = built
    return service, signals_engine


def _build_knowledge(settings: Settings) -> tuple[KnowledgeService | None, Engine | None]:
    """Open ``knowledge.db`` and construct the knowledge service, or return ``(None, None)``.

    Best-effort by design: a deployment that has not run ``c360 ingest-knowledge``, or one where
    ``sqlite-vec`` cannot load, still serves the whole deterministic platform. The knowledge
    endpoints and tool then report "not available" rather than the app failing to build. Only a
    genuinely broken (present-but-unreadable) database would propagate, which is the same policy the
    customer engine applies.
    """
    from c360.knowledge.embeddings import build_embedding_provider  # noqa: PLC0415
    from c360.knowledge.engine import create_knowledge_engine  # noqa: PLC0415
    from c360.knowledge.extension import VecExtensionError, vec_available  # noqa: PLC0415
    from c360.knowledge.repository import KnowledgeRepository  # noqa: PLC0415
    from c360.knowledge.rerank import build_reranker  # noqa: PLC0415
    from c360.knowledge.retrieval import KnowledgeRetriever, RetrievalConfig  # noqa: PLC0415

    if not settings.knowledge_db_path.is_file() or not vec_available():
        return None, None
    try:
        knowledge_engine = create_knowledge_engine(
            settings.knowledge_db_path,
            mode=AccessMode.READ_ONLY,
            pool_size=settings.sqlite_read_pool_size,
        )
    except (DatabaseFileMissingError, VecExtensionError):
        return None, None

    from c360.agents.breaker import CircuitBreaker  # noqa: PLC0415 - avoid import cycle at load
    from c360.agents.degrade import (  # noqa: PLC0415
        BreakerEmbeddingProvider,
        BreakerReranker,
    )
    from c360.core.telemetry import breaker_transition_hook  # noqa: PLC0415

    repository = KnowledgeRepository(knowledge_engine)
    # Wrap the query embedder and reranker in circuit breakers (task 8.11, design §13.7): a
    # repeatedly-failing embedder trips its breaker and retrieval degrades to lexical-only; a bad
    # reranker degrades to fusion order. The retriever owns each fallback. The transition hook feeds
    # the breaker metric and the breaker-open alert (task 10.4).
    embedder = BreakerEmbeddingProvider(
        build_embedding_provider(settings),
        CircuitBreaker("embedding", on_transition=breaker_transition_hook),
    )
    reranker = BreakerReranker(
        build_reranker(settings),
        CircuitBreaker("rerank", on_transition=breaker_transition_hook),
    )
    retriever = KnowledgeRetriever(
        repository,
        embedder,
        RetrievalConfig(
            lexical_k=settings.retrieval_lexical_k,
            semantic_k=settings.retrieval_semantic_k,
            rrf_k=settings.retrieval_rrf_k,
            context_chunks=settings.retrieval_context_chunks,
            min_score=settings.retrieval_min_score,
            embed_dimensions=settings.bedrock_embed_dimensions,
        ),
        reranker,
    )
    return KnowledgeService(retriever, repository), knowledge_engine


# One lock per process guards the lazy build so two concurrent first requests do not each open an
# engine. The double-checked read keeps the steady-state path lock-free after the first build.
_BUILD_LOCK = threading.Lock()


def get_services(request: Request) -> Services | None:
    """Return the cached service container, building it once on first use, or ``None`` if unbuilt.

    ``None`` is returned only when the customer database does not exist; every other failure to open
    it propagates, because a corrupt or unreadable-but-present database is an operational fault to
    surface, not a not-yet-seeded state to tolerate.
    """
    cached: Services | None = getattr(request.app.state, "services", None)
    if isinstance(cached, Services):
        return cached
    with _BUILD_LOCK:
        cached = getattr(request.app.state, "services", None)
        if isinstance(cached, Services):
            return cached
        try:
            services = build_services(request.app.state.settings)
        except DatabaseFileMissingError:
            return None
        request.app.state.services = services
        return services


def require_services(request: Request) -> Services:
    """FastAPI dependency: the service container, or a 503 when the database is not available."""
    services = get_services(request)
    if services is None:
        raise ApiError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "the customer database is not available; seed it before querying",
        )
    return services


def dispose_services(app_state: object) -> None:
    """Dispose the engine at shutdown if one was built, releasing the connection pool."""
    services: Services | None = getattr(app_state, "services", None)
    if services is not None:
        services.engine.dispose()
        if services.knowledge_engine is not None:
            services.knowledge_engine.dispose()
        if services.signals_engine is not None:
            services.signals_engine.dispose()
        if services.reports_engine is not None:
            services.reports_engine.dispose()


__all__ = [
    "Services",
    "build_services",
    "dispose_services",
    "get_services",
    "require_services",
]
