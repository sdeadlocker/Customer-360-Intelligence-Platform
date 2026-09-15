"""Signal prioritization and ranking (task 17.3).

A worklist is only useful if the most important thing is at the top. The score combines three terms
an RM would weigh by hand:

* **severity** — CRITICAL outranks WARNING outranks INFO;
* **value-at-stake** — a signal on a large exposure or a large deposit matters more than one on a
  trivial amount, normalized against a configured cap so a single outlier cannot dominate;
* **recency** — a freshly-detected signal edges out an older one of equal weight.

``score = severity_w · severity_norm + value_w · value_norm + recency_w · recency_norm``, each term
normalized to 0..1 before weighting so the weights mean what an operator expects and can be retuned
from config without a code change (design §18). Ordering is **stable**: ties break on descending
value-at-stake then ascending customer id then dedup key, so the same inputs always produce the same
queue — a property the reproducibility test asserts.

Entitlement scoping and suppression are applied by the service layer (:mod:`c360.signals.service`)
and the query, not here — the ranker is a pure function of a signal and the run's ``as_of`` so it is
trivially testable. Dismissed and cooled-off signals never reach it because they are filtered out
first (task 17.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from c360.signals.models import DetectedSignal, Severity

#: Severity normalized to 0..1 — the three-point IntEnum mapped onto the unit interval.
_SEVERITY_NORM: dict[Severity, float] = {
    Severity.INFO: 0.34,
    Severity.WARNING: 0.67,
    Severity.CRITICAL: 1.0,
}

#: A recency floor so an old signal still carries a fraction of the recency term rather than zero;
#: the term decays linearly from 1.0 (detected today) to this floor at the horizon.
_RECENCY_FLOOR = 0.1
#: Days over which the recency term decays to the floor. A signal older than this scores the floor.
_RECENCY_HORIZON_DAYS = 60


@dataclass(frozen=True, slots=True)
class RankingWeights:
    """The three configurable weights (task 17.3).

    Plain floats, validated non-negative, so an operator retunes the queue via config
    (``SIGNAL_RANK_*_WEIGHT``) without touching code. ``value_cap_cents`` is the ceiling the
    value-at-stake term normalizes against.
    """

    severity_weight: float
    value_weight: float
    recency_weight: float
    value_cap_cents: int

    def __post_init__(self) -> None:
        if self.severity_weight < 0 or self.value_weight < 0 or self.recency_weight < 0:
            raise ValueError("ranking weights must be non-negative")
        if self.value_cap_cents < 1:
            raise ValueError("value_cap_cents must be positive")


def _recency_norm(signal_as_of: date, run_as_of: date) -> float:
    """Linear decay from 1.0 (same day) to :data:`_RECENCY_FLOOR` at the horizon.

    ``run_as_of`` is the batch's reference date, not wall-clock, so a run over a fixed seeded
    dataset scores identically every time — recency is measured against the data's as-of, which is
    what keeps detection reproducible (task 17.2, 17.7).
    """
    age_days = max(0, (run_as_of - signal_as_of).days)
    if age_days >= _RECENCY_HORIZON_DAYS:
        return _RECENCY_FLOOR
    span = 1.0 - _RECENCY_FLOOR
    return 1.0 - span * (age_days / _RECENCY_HORIZON_DAYS)


def score_signal(signal: DetectedSignal, run_as_of: date, weights: RankingWeights) -> float:
    """Compute the ranking score for one signal (task 17.3). Always non-negative."""
    severity_norm = _SEVERITY_NORM[signal.severity]
    value_norm = min(1.0, signal.value_at_stake_cents / weights.value_cap_cents)
    recency_norm = _recency_norm(signal.as_of, run_as_of)
    return (
        weights.severity_weight * severity_norm
        + weights.value_weight * value_norm
        + weights.recency_weight * recency_norm
    )


def rank(
    signals: list[DetectedSignal], run_as_of: date, weights: RankingWeights
) -> list[tuple[DetectedSignal, float]]:
    """Score and stably order signals, most important first (task 17.3).

    Returns ``(signal, score)`` pairs sorted by descending score, with ties broken deterministically
    on value-at-stake (desc), customer id (asc) then dedup key (asc). The stable tiebreak is what
    makes the queue reproducible across runs on the same data.
    """
    scored = [(signal, score_signal(signal, run_as_of, weights)) for signal in signals]
    scored.sort(
        key=lambda pair: (
            -pair[1],
            -pair[0].value_at_stake_cents,
            pair[0].customer_id,
            pair[0].dedup_key,
        )
    )
    return scored


__all__ = ["RankingWeights", "rank", "score_signal"]
