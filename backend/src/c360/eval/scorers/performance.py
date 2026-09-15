"""Latency, cost and stability -- dimensions 14 and 15 (task 11.9, design §14.3).

Both dimensions are advisory: design §14.3 marks stability "reported, advisory" and latency/cost is
a
budget report, not a hard gate (a slow or expensive run is a signal to investigate, not a merge
blocker). They exist so a champion/challenger comparison can weigh a quality gain against a latency
or cost regression.

* **Latency and cost (14)** -- p50/p95 of the per-customer dashboard latency (the full-graph wall
  clock the design §13 budget is written against), plus an estimated cost per run derived from token
  usage against ``config/bedrock_prices.json``. The mock reports zero-cost tokens (its model
  id is absent from the price table, which is treated as free), so on the mock the cost is zero and
  the latency reflects the deterministic template render -- both honest, both useful as a floor.
* **Stability (15)** -- output variance across N repeated runs of the same customers. The mock is
  deterministic, so its variance is exactly zero, which is the correct baseline; a live model's
  variance is the interesting number and is reported for the full run.

Cost estimation without per-agent token usage
----------------------------------------------

The :class:`~c360.agents.result.AgentResult` the harness collects does not carry token counts (they
live on the provider response inside the runner). Rather than thread usage through the frozen result
type, cost is estimated from the produced narrative with the same ~4-characters-per-token heuristic
the mock uses, priced at the *output* rate for the run's model. This is an estimate, labelled as
such; the authoritative per-call cost lives in the ``c360.model.cost`` metric (task 10.2). For the
mock the estimate is zero because the model is unpriced, which is the right answer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from c360.eval.results import DimensionScore

if TYPE_CHECKING:
    from pathlib import Path

    from c360.eval.harness import EvalHarness, HarnessOutput

#: The mock's token heuristic (provider.py ``_estimate_tokens``): ~4 characters to a token.
_CHARS_PER_TOKEN = 4

#: Default dashboard latency budget (ms) design §6.3 gives the insights view (5 s p95).
_DASHBOARD_P95_BUDGET_MS = 5000.0


async def score_latency_cost(
    harness: EvalHarness, price_table_path: Path, *, budget_p95_ms: float = _DASHBOARD_P95_BUDGET_MS
) -> DimensionScore:
    """Dimension 14: p50/p95 dashboard latency and estimated cost per run (advisory).

    Runs one timed dashboard pass over the panel. ``value`` is the p95 latency in milliseconds so a
    report and a champion/challenger diff have a single headline number; the p50 and the cost
    estimate travel in the detail. ``passed`` reflects the p95 budget but the dimension is advisory,
    so a breach is reported, not gated.
    """
    output, elapsed_ms = await harness.run_dashboard_timed()
    latencies = sorted(elapsed_ms.values())
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)

    prices = _load_prices(price_table_path)
    cost_micro_usd = _estimate_cost_micro_usd(output, harness.model_id, prices)

    return DimensionScore(
        dimension=14,
        name="Latency and cost",
        value=p95,
        threshold=budget_p95_ms,
        hard_gate=False,
        passed=p95 <= budget_p95_ms,
        detail={
            "p50_ms": p50,
            "p95_ms": p95,
            "customers": len(latencies),
            "estimated_cost_micro_usd": cost_micro_usd,
            "model_id": harness.model_id,
            "cost_is_estimate": True,
        },
    )


async def score_stability(
    harness: EvalHarness, *, repeats: int = 2, sample: int = 4
) -> DimensionScore:
    """Dimension 15: output variance across ``repeats`` runs of a sample of customers (advisory).

    Variance is the share of (customer, agent) narratives that differed across the repeated runs. A
    deterministic provider scores exactly 0.0 (perfect stability); a live model's non-zero figure is
    the reported signal. Only a sample of the panel is repeated, because stability is a property of
    the generation process, not of any particular customer, and repeating the whole panel N times is
    needless cost.
    """
    outputs: list[HarnessOutput] = []
    for _ in range(max(1, repeats)):
        outputs.append(await harness.run_dashboard())

    # Compare narratives keyed by (customer, agent) across the runs, limited to the sampled prefix.
    reference = outputs[0]
    sampled_customers = _sampled_customers(reference, sample)
    total = 0
    varied = 0
    for customer_id in sampled_customers:
        for run in reference.for_customer(customer_id):
            total += 1
            baseline = run.narrative
            for other in outputs[1:]:
                match = _find(other, customer_id, run.agent)
                if match is None or match != baseline:
                    varied += 1
                    break
    variance = 0.0 if total == 0 else varied / total
    return DimensionScore(
        dimension=15,
        name="Stability",
        value=variance,
        threshold=None,
        hard_gate=False,
        passed=True,  # advisory: never blocks (design §14.3)
        detail={
            "repeats": max(1, repeats),
            "sampled": len(sampled_customers),
            "compared": total,
            "varied": varied,
            "variance": variance,
        },
    )


# ---------------------------------------------------------------- helpers


def _sampled_customers(output: HarnessOutput, sample: int) -> list[str]:
    seen: list[str] = []
    for run in output.agent_runs:
        if run.customer_id not in seen:
            seen.append(run.customer_id)
        if len(seen) >= sample:
            break
    return seen


def _find(output: HarnessOutput, customer_id: str, agent: str) -> str | None:
    for run in output.for_customer(customer_id):
        if run.agent == agent:
            return run.narrative
    return None


def _estimate_cost_micro_usd(
    output: HarnessOutput, model_id: str, prices: dict[str, dict[str, int]]
) -> int:
    """Estimate the run's generation cost in micro-USD from produced narrative length.

    An unpriced model (the mock) contributes zero. The estimate uses the output-token rate only, as
    the narrative is the output; input-side cost is not reconstructable from the result and is small
    relative to output for these prompts.
    """
    model_price = prices.get(model_id)
    if model_price is None:
        return 0
    output_rate = model_price.get("output", 0)
    output_tokens = 0
    for run in output.agent_runs:
        output_tokens += max(1, len(run.narrative) // _CHARS_PER_TOKEN)
    return (output_tokens * output_rate) // 1000


def _load_prices(path: Path) -> dict[str, dict[str, int]]:
    """Load the Bedrock price table, or an empty table when it is missing."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    models = raw.get("models", {})
    return models if isinstance(models, dict) else {}


def _percentile(sorted_values: list[float], percentile: int) -> float:
    """Nearest-rank percentile of an already-sorted list; 0.0 for an empty list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(1, math_ceil(percentile / 100 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def math_ceil(value: float) -> int:
    """Local ceil to avoid importing math for a single call."""
    return int(value) if value == int(value) else int(value) + 1


__all__ = ["score_latency_cost", "score_stability"]
