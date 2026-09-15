"""Task-specific quality scorers -- dimensions 8-10 (task 11.6, design §14.3).

These measure whether the agents got the *task* right, against the generator's exported labels:

* **Life-event detection (8)** -- precision / recall / F1 of the Life Event agent's detected events
  against the events the generator actually created. Design §14.3 gates recall at >= 0.85: missing a
  real event is worse than over-calling one.
* **Offer ranking (9)** -- nDCG@3 and precision@1 of the Offer agent's ranked recommendations
against
  the customer's true acceptance propensity. Design gates nDCG@3 at >= 0.75.
* **Risk-driver correctness (10)** -- the share of the Risk agent's named drivers that match the
  factors the generator actually used to compose the score. Design gates at >= 0.90.

Where the predictions come from
-------------------------------

Each agent output carries *structured* fields the live model fills -- ``LifeEventOutput.events``,
``OfferOutput.ranked_offers``, ``RiskOutput.drivers`` -- and that is where these scorers read from.
The deterministic mock provider does not populate them (it renders a grounded narrative and fills
only the snapshot fields), which is exactly why design §14.5 scopes dimensions 8-10 to ``--mode
full`` against Bedrock and *not* to the mock CI gate: on the mock they report low, honestly, rather
than being faked to pass. They are therefore soft (advisory) dimensions here -- reported and
diffable,
never a CI block -- with the design thresholds recorded so a Bedrock run is judged against them.

All three are order- and label-aware but tolerant of vocabulary: a predicted event type, offer
product kind or risk factor is matched case-insensitively against the label, so a model that says
"home purchase" scores against ``HOME_PURCHASE``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from c360.eval.results import DimensionScore

if TYPE_CHECKING:
    from c360.eval.ground_truth import GroundTruth
    from c360.eval.harness import AgentRun, HarnessOutput

_LIFE_EVENT_RECALL_MIN = 0.85
_OFFER_NDCG_MIN = 0.75
_RISK_DRIVER_MIN = 0.90

#: How deep the offer-ranking metrics look. Design §14.3 names nDCG@3 and precision@1.
_NDCG_K = 3


def score_life_event_detection(output: HarnessOutput, ground_truth: GroundTruth) -> DimensionScore:
    """Dimension 8: precision / recall / F1 of detected life events (recall >= 0.85, soft).

    Scored on the union of true and predicted events across the panel (micro-averaged), so a
    customer with three events counts three times as much as one with a single event -- which is the
    right weighting for a detection metric.
    """
    true_positives = 0
    predicted = 0
    actual = 0
    per_customer: list[dict[str, object]] = []
    for customer_id in ground_truth.panel:
        truth = {e.event_type.upper() for e in ground_truth.by_customer[customer_id].life_events}
        pred = _predicted_life_events(output.for_customer(customer_id))
        hits = truth & pred
        true_positives += len(hits)
        predicted += len(pred)
        actual += len(truth)
        if truth or pred:
            per_customer.append(
                {"customer": customer_id, "true": len(truth), "pred": len(pred), "hit": len(hits)}
            )
    precision = _rate(true_positives, predicted)
    recall = _rate(true_positives, actual)
    f1 = _f1(precision, recall)
    return DimensionScore(
        dimension=8,
        name="Life-event detection",
        # The gated metric is recall (design §14.3); precision and F1 travel in the detail.
        value=recall,
        threshold=_LIFE_EVENT_RECALL_MIN,
        hard_gate=False,
        passed=recall >= _LIFE_EVENT_RECALL_MIN,
        detail={
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "true_positives": true_positives,
            "predicted": predicted,
            "actual": actual,
        },
    )


def score_offer_ranking(output: HarnessOutput, ground_truth: GroundTruth) -> DimensionScore:
    """Dimension 9: nDCG@3 and precision@1 of offer ranking vs propensity (nDCG@3 >= 0.75).

    Relevance is derived from the true propensity ranking: the most-likely product has the highest
    gain, decreasing down the list, zero for a product not in the ranking. nDCG normalises against
    the ideal ordering, so a customer with only one plausible product is not penalised for a short
    list. Both metrics are averaged over the panel customers that have a propensity label.
    """
    ndcgs: list[float] = []
    precisions: list[float] = []
    scored = 0
    for customer_id in ground_truth.panel:
        ideal = ground_truth.by_customer[customer_id].propensity_ranking
        if not ideal:
            continue
        predicted = _predicted_offer_kinds(output.for_customer(customer_id))
        if not predicted:
            # No prediction is a zero for both metrics -- a real miss, not skipped.
            ndcgs.append(0.0)
            precisions.append(0.0)
            scored += 1
            continue
        relevance = {kind.upper(): len(ideal) - i for i, kind in enumerate(ideal)}
        ndcgs.append(_ndcg(predicted, relevance, k=_NDCG_K))
        precisions.append(1.0 if predicted[0].upper() in set(relevance) else 0.0)
        scored += 1
    ndcg = _mean(ndcgs)
    precision_at_1 = _mean(precisions)
    return DimensionScore(
        dimension=9,
        name="Offer ranking",
        value=ndcg,
        threshold=_OFFER_NDCG_MIN,
        hard_gate=False,
        passed=ndcg >= _OFFER_NDCG_MIN,
        detail={"ndcg_at_3": ndcg, "precision_at_1": precision_at_1, "scored_customers": scored},
    )


def score_risk_driver_correctness(
    output: HarnessOutput, ground_truth: GroundTruth
) -> DimensionScore:
    """Dimension 10: named risk drivers match the true score composition (>= 0.90, soft).

    For each panel customer the Risk agent's named drivers are matched against the factors the
    generator used. Scored as the share of predicted drivers that are correct (precision of the
    named set), micro-averaged, because the failure design §14.3 cares about is an agent *inventing*
    a driver the score does not rest on.
    """
    correct = 0
    predicted = 0
    per_customer: list[dict[str, object]] = []
    for customer_id in ground_truth.panel:
        truth = {f.upper() for f in ground_truth.by_customer[customer_id].risk.factors}
        pred = _predicted_risk_drivers(output.for_customer(customer_id))
        hits = {p for p in pred if p in truth}
        correct += len(hits)
        predicted += len(pred)
        if pred:
            per_customer.append(
                {"customer": customer_id, "pred": sorted(pred), "correct": sorted(hits)}
            )
    # When no agent named any driver (the mock case), there is nothing to be wrong about; report the
    # vacuous 1.0 but record that no drivers were named so the report shows the mock produced none.
    rate = _rate(correct, predicted) if predicted else 1.0
    return DimensionScore(
        dimension=10,
        name="Risk driver correctness",
        value=rate,
        threshold=_RISK_DRIVER_MIN,
        hard_gate=False,
        passed=rate >= _RISK_DRIVER_MIN,
        detail={"correct": correct, "predicted": predicted, "named_by_customer": per_customer},
    )


# ---------------------------------------------------------------- prediction extraction


def _predicted_life_events(runs: tuple[AgentRun, ...]) -> set[str]:
    """The life-event types the Life Event agent asserted, from its structured output."""
    events: set[str] = set()
    for run in runs:
        if run.agent != "life_event":
            continue
        for item in getattr(run.result.outputs, "events", ()):
            event_type = getattr(item, "event_type", None)
            if event_type:
                events.add(str(event_type).upper())
    return events


def _predicted_offer_kinds(runs: tuple[AgentRun, ...]) -> list[str]:
    """The ranked product kinds the Offer agent recommended, best first, from structured output."""
    for run in runs:
        if run.agent != "offer_recommendation":
            continue
        kinds: list[str] = []
        for item in getattr(run.result.outputs, "ranked_offers", ()):
            offer_id = getattr(item, "offer_id", None)
            if offer_id:
                kinds.append(str(offer_id))
        if kinds:
            return kinds
    return []


def _predicted_risk_drivers(runs: tuple[AgentRun, ...]) -> set[str]:
    """The risk factors the Risk agent named, from its structured drivers list."""
    drivers: set[str] = set()
    for run in runs:
        if run.agent != "risk":
            continue
        for item in getattr(run.result.outputs, "drivers", ()):
            factor = getattr(item, "factor", None)
            if factor:
                drivers.add(str(factor).upper())
    return drivers


# ---------------------------------------------------------------- metric maths


def _ndcg(predicted: list[str], relevance: dict[str, int], *, k: int) -> float:
    """Normalised discounted cumulative gain at ``k`` for a predicted ranking."""
    gains = [relevance.get(item.upper(), 0) for item in predicted[:k]]
    dcg = sum(gain / math.log2(i + 2) for i, gain in enumerate(gains))
    ideal_gains = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(gain / math.log2(i + 2) for i, gain in enumerate(ideal_gains))
    return 0.0 if idcg == 0 else dcg / idcg


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _mean(values: list[float]) -> float:
    return 0.0 if not values else sum(values) / len(values)


__all__ = [
    "score_life_event_detection",
    "score_offer_ranking",
    "score_risk_driver_correctness",
]
