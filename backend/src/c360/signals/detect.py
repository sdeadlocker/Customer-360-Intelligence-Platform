"""The batch signal-detection job (task 17.4).

Runs every detector over every customer in the read-only ``customer.db``, ranks the results and
writes them to the writable ``signals.db`` in one idempotent pass. Idempotent because every signal
carries a deterministic ``dedup_key`` the store upserts on (task 17.1): running the job twice over
unchanged data leaves the same rows with the same scores, and running it after a recompute updates
severities and scores in place. A ``signal_run`` provenance row is written so a run is inspectable
and safe to schedule (``c360 detect-signals`` / ``POST /admin/detect-signals``).

Reproducibility and read-only safety
------------------------------------

The customer engine is opened ``mode=ro`` exactly as the API opens it, so the job cannot write a
customer row even by mistake — the read-only proof in task 17.7 asserts this. The run's ``as_of`` is
read from the data (``customer.as_of_date``), not from wall-clock, so recency scoring and therefore
the whole queue are reproducible on a fixed seeded dataset.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from c360.core.logging import get_logger
from c360.core.telemetry import get_meter, get_tracer
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.repositories import build_repositories
from c360.services.financial import FinancialService
from c360.services.journey import JourneyService
from c360.services.risk import RiskService
from c360.signals import detectors
from c360.signals.models import DetectedSignal
from c360.signals.ranking import RankingWeights, rank
from c360.signals.store import RunProvenance, SignalStore

_logger = get_logger(__name__)

_TRACER_NAME = "c360.signals"


class _SignalMetrics:
    """Lazily-created signal instruments, so a disabled telemetry pipeline costs nothing.

    A single counter labelled by ``signal_type`` and ``severity`` (both bounded closed sets, allowed
    metric labels) records how many signals of each kind a run produced. No customer id, no monetary
    value and no free text is ever a label (design §18.8) — the whole point of routing through
    ``scrub_labels`` is that a stray key is dropped rather than exported.
    """

    __slots__ = ("_detected", "_scrub")

    def __init__(self) -> None:
        from c360.core.telemetry import scrub_labels  # noqa: PLC0415

        self._scrub = scrub_labels
        meter = get_meter(_TRACER_NAME)
        self._detected = meter.create_counter(
            "c360.signals.detected",
            description="Signals detected, labelled by type and severity.",
        )

    def record(self, signal_type: str, severity: str, count: int) -> None:
        if count:
            self._detected.add(
                count, self._scrub({"signal_type": signal_type, "severity": severity})
            )


@dataclass(frozen=True, slots=True)
class DetectConfig:
    """The tunables the job needs, threaded from :class:`~c360.core.config.Settings`."""

    large_deposit_threshold_cents: int
    spend_anomaly_sigma: float
    major_txn_absolute_threshold_cents: int
    major_txn_median_multiple: float
    weights: RankingWeights


def _as_of(engine: object) -> date:
    """The dataset's as-of date, read from ``customer`` (mirrors the recompute job's ``_as_of``)."""
    from sqlalchemy import text  # noqa: PLC0415

    with engine.connect() as connection:  # type: ignore[attr-defined]
        row = connection.execute(text("SELECT max(as_of_date) FROM customer")).fetchone()
    if row is None or row[0] is None:
        from datetime import UTC, datetime  # noqa: PLC0415

        return datetime.now(UTC).date()
    return date.fromisoformat(str(row[0]))


def _detect_for_customer(
    customer: object,
    *,
    risk: RiskService,
    financial: FinancialService,
    journey: JourneyService,
    as_of: date,
    config: DetectConfig,
) -> Iterator[DetectedSignal]:
    """Run every detector for one customer, yielding each signal it produces."""
    yield from detectors.detect_risk_band(customer, risk, as_of)  # type: ignore[arg-type]
    yield from detectors.detect_aml_pep(customer, risk, as_of)  # type: ignore[arg-type]
    yield from detectors.detect_large_deposit(
        customer,  # type: ignore[arg-type]
        financial,
        as_of,
        threshold_cents=config.large_deposit_threshold_cents,
    )
    yield from detectors.detect_life_event(customer, journey, as_of)  # type: ignore[arg-type]


def detect_signals(
    *,
    customer_db_path: Path,
    signals_db_path: Path,
    config: DetectConfig,
) -> RunProvenance:
    """Detect, rank and persist signals for every customer. Idempotent (task 17.4).

    Returns the run's provenance (id and counts), which the CLI prints and tests assert on.
    """
    tracer = get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span("detect_signals") as span:
        provenance = _detect_signals_traced(
            customer_db_path=customer_db_path, signals_db_path=signals_db_path, config=config
        )
        # Bounded, non-identifying attributes only: counts, never a customer id or a value.
        span.set_attribute("c360.signals.customers_scanned", provenance.customers_scanned)
        span.set_attribute("c360.signals.signals_written", provenance.signals_written)
    return provenance


def _detect_signals_traced(
    *,
    customer_db_path: Path,
    signals_db_path: Path,
    config: DetectConfig,
) -> RunProvenance:
    metrics = _SignalMetrics()
    engine = create_sqlite_engine(customer_db_path, mode=AccessMode.READ_ONLY, pool_size=4)
    try:
        repositories = build_repositories(engine)
        risk = RiskService(repositories.risk)
        financial = FinancialService(
            repositories.financial, spend_anomaly_sigma=config.spend_anomaly_sigma
        )
        journey = JourneyService(
            repositories.journey,
            repositories.relationship,
            major_txn_absolute_threshold_cents=config.major_txn_absolute_threshold_cents,
            major_txn_median_multiple=config.major_txn_median_multiple,
        )
        as_of = _as_of(engine)
        customers = repositories.customer.list_all()

        detected: list[DetectedSignal] = []
        for customer in customers:
            detected.extend(
                _detect_for_customer(
                    customer,
                    risk=risk,
                    financial=financial,
                    journey=journey,
                    as_of=as_of,
                    config=config,
                )
            )
    finally:
        engine.dispose()

    ranked = rank(detected, as_of, config.weights)

    store = SignalStore(signals_db_path)
    run_id = store.begin_run(as_of=as_of.isoformat(), source_db=str(customer_db_path))
    signals = [pair[0] for pair in ranked]
    scores = [pair[1] for pair in ranked]
    written = store.upsert_signals(run_id, signals, scores=scores)
    store.finish_run(run_id, customers_scanned=len(customers), signals_written=written)

    # Metric: signals by type and severity (task 17.7). Aggregated across the run so one counter
    # add per (type, severity) bucket rather than per signal.
    counts: dict[tuple[str, str], int] = {}
    for sig in signals:
        key = (str(sig.signal_type), sig.severity.name)
        counts[key] = counts.get(key, 0) + 1
    for (signal_type, severity), count in counts.items():
        metrics.record(signal_type, severity, count)

    _logger.info(
        "signal detection complete",
        extra={"run_id": run_id, "scanned": len(customers), "written": written},
    )
    return RunProvenance(
        run_id=run_id,
        as_of=as_of.isoformat(),
        customers_scanned=len(customers),
        signals_written=written,
    )


def detect_config_from_settings(settings: object) -> DetectConfig:
    """Build a :class:`DetectConfig` from application settings."""
    return DetectConfig(
        large_deposit_threshold_cents=settings.signal_large_deposit_threshold_cents,  # type: ignore[attr-defined]
        spend_anomaly_sigma=settings.spend_anomaly_sigma,  # type: ignore[attr-defined]
        major_txn_absolute_threshold_cents=settings.major_txn_absolute_threshold_cents,  # type: ignore[attr-defined]
        major_txn_median_multiple=settings.major_txn_median_multiple,  # type: ignore[attr-defined]
        weights=RankingWeights(
            severity_weight=settings.signal_rank_severity_weight,  # type: ignore[attr-defined]
            value_weight=settings.signal_rank_value_weight,  # type: ignore[attr-defined]
            recency_weight=settings.signal_rank_recency_weight,  # type: ignore[attr-defined]
            value_cap_cents=settings.signal_value_at_stake_cap_cents,  # type: ignore[attr-defined]
        ),
    )


__all__ = ["DetectConfig", "detect_config_from_settings", "detect_signals"]
