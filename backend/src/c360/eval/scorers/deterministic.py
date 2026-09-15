"""Deterministic scorers -- dimensions 1-5 (task 11.3, design §14.3).

These five are pure assertions with no model in the loop, which is exactly why they run as hard
gates in CI (design §14.1). They read the harness's :class:`~c360.eval.harness.AgentRun` records --
each an agent's :class:`~c360.agents.result.AgentResult` plus the
:class:`~c360.tools.facts.FactTable` it was grounded on -- and, for coverage, the exported
:class:`~c360.eval.ground_truth.GroundTruth`.

* **Schema conformance (1, hard 100%)** -- every agent output re-validates against its Pydantic
  model. The graph already returns validated models, so re-validating a serialised round-trip is the
  belt-and-braces check design §14.3 names: a run cannot be scored on output that would not survive
  the API's own serialisation.
* **Groundedness (2, hard 100%)** -- the share of agent narratives whose every numeric claim
resolves
to a fact within tolerance, via the same :func:`~c360.agents.validator.validate_claims` the runtime
gates on. A degraded (template-fallback) card is grounded by construction, so this measures that the
  grounding contract held end to end.
* **Numeric-claim provenance (3, hard 100%)** -- no figure sourced *only* from a retrieved passage.
  The validator already rejects passage-cited numbers; this dimension isolates that specific
  violation class so a provenance breach is reported distinctly from a plain fabrication.
* **Citation validity (4, >=98%)** -- every ``[F..]`` marker resolves to a real fact in
  that agent's table. A dangling citation is a broken link in the UI even when the figure happens to
  be right.
* **Material-fact coverage (5, >=95%)** -- for each panel customer, the material facts the generator
  actually created (90+ DPD, AML/PEP, fraud alert, maturing deposit) are surfaced somewhere in that
  customer's agent narratives, matched by the label's keywords.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from c360.agents.validator import validate_claims
from c360.eval.results import DimensionScore

if TYPE_CHECKING:
    from c360.eval.ground_truth import GroundTruth
    from c360.eval.harness import AgentRun, HarnessOutput

#: A citation marker in a narrative: ``[F12]``. Only fact markers matter for citation validity.
_FACT_MARKER: Final = re.compile(r"\[F(\d+)\]")

#: Gate thresholds from design §14.3.
_GROUNDEDNESS_MIN: Final = 1.0
_PROVENANCE_MIN: Final = 1.0
_CITATION_MIN: Final = 0.98
_COVERAGE_MIN: Final = 0.95


def score_schema_conformance(output: HarnessOutput) -> DimensionScore:
    """Dimension 1: every agent output validates against its schema (hard, 100%)."""
    total = 0
    failures: list[dict[str, str]] = []
    for run in output.agent_runs:
        total += 1
        model = run.result.outputs
        try:
            type(model).model_validate(model.model_dump())
        except ValidationError as exc:
            failures.append(
                {"customer": run.customer_id, "agent": run.agent, "error": exc.__class__.__name__}
            )
    rate = _rate(total - len(failures), total)
    return DimensionScore(
        dimension=1,
        name="Schema conformance",
        value=rate,
        threshold=1.0,
        hard_gate=True,
        passed=rate >= 1.0,
        detail={"total": total, "failures": failures},
    )


def score_groundedness(output: HarnessOutput) -> DimensionScore:
    """Dimension 2: share of narratives with every numeric claim grounded (hard, 100%)."""
    total = 0
    ungrounded: list[dict[str, object]] = []
    for run in output.agent_runs:
        total += 1
        verdict = validate_claims(run.narrative, run.facts)
        if not verdict.ok:
            ungrounded.append(
                {
                    "customer": run.customer_id,
                    "agent": run.agent,
                    "violations": [v.reason for v in verdict.violations],
                }
            )
    rate = _rate(total - len(ungrounded), total)
    return DimensionScore(
        dimension=2,
        name="Groundedness",
        value=rate,
        threshold=_GROUNDEDNESS_MIN,
        hard_gate=True,
        passed=rate >= _GROUNDEDNESS_MIN,
        detail={"total": total, "ungrounded": ungrounded},
    )


def score_numeric_provenance(output: HarnessOutput) -> DimensionScore:
    """Dimension 3: no figure sourced only from a passage (hard, 100%).

    Isolates the passage-provenance violation from all other claim violations, because design §14.3
    tracks it as its own gate (RAG must never supply a customer figure -- design §9.1).
    """
    total = 0
    breaches: list[dict[str, object]] = []
    for run in output.agent_runs:
        total += 1
        verdict = validate_claims(run.narrative, run.facts)
        passage_sourced = [v for v in verdict.violations if "passage" in v.reason]
        if passage_sourced:
            breaches.append(
                {
                    "customer": run.customer_id,
                    "agent": run.agent,
                    "figures": [v.number for v in passage_sourced],
                }
            )
    rate = _rate(total - len(breaches), total)
    return DimensionScore(
        dimension=3,
        name="Numeric-claim provenance",
        value=rate,
        threshold=_PROVENANCE_MIN,
        hard_gate=True,
        passed=rate >= _PROVENANCE_MIN,
        detail={"total": total, "breaches": breaches},
    )


def score_citation_validity(output: HarnessOutput) -> DimensionScore:
    """Dimension 4: every ``[F..]`` marker in a narrative resolves to a real fact (>=98%).

    Scored per marker, not per narrative, so one dangling citation in an otherwise-good card lowers
    the rate proportionally rather than failing the whole card -- what a ``>=98%`` gate on a
    soft dimension expects.
    """
    total = 0
    dangling: list[dict[str, str]] = []
    for run in output.agent_runs:
        fact_ids = {fact.fact_id for fact in run.facts.facts}
        for marker in _FACT_MARKER.finditer(run.narrative):
            total += 1
            fact_id = f"F{marker.group(1)}"
            if fact_id not in fact_ids:
                dangling.append(
                    {"customer": run.customer_id, "agent": run.agent, "fact_id": fact_id}
                )
    # A run with no cited figures is vacuously valid -- nothing to resolve, so no failure to score.
    resolved = total - len(dangling)
    rate = _rate(resolved, total) if total else 1.0
    return DimensionScore(
        dimension=4,
        name="Citation validity",
        value=rate,
        threshold=_CITATION_MIN,
        hard_gate=False,
        passed=rate >= _CITATION_MIN,
        detail={"markers": total, "dangling": dangling},
    )


def score_material_fact_coverage(
    output: HarnessOutput, ground_truth: GroundTruth
) -> DimensionScore:
    """Dimension 5: required material facts are surfaced in a customer's narratives (>=95%).

    A material fact is "covered" when any of its keywords appears in the concatenated narratives of
    that customer's agents. Only customers with at least one material fact contribute, so a clean
    panel customer neither helps nor penalises the score; a panel with no material facts at all
    scores a vacuous 1.0 (nothing was required, nothing was missed).
    """
    required = 0
    covered = 0
    misses: list[dict[str, str]] = []
    for customer_id in ground_truth.panel:
        labels = ground_truth.by_customer[customer_id].material_facts
        if not labels:
            continue
        narrative = _combined_narrative(output.for_customer(customer_id)).lower()
        for fact in labels:
            required += 1
            if any(keyword.lower() in narrative for keyword in fact.keywords):
                covered += 1
            else:
                misses.append({"customer": customer_id, "kind": fact.kind})
    rate = _rate(covered, required) if required else 1.0
    return DimensionScore(
        dimension=5,
        name="Material-fact coverage",
        value=rate,
        threshold=_COVERAGE_MIN,
        hard_gate=False,
        passed=rate >= _COVERAGE_MIN,
        detail={"required": required, "covered": covered, "misses": misses},
    )


# ---------------------------------------------------------------- helpers


def _combined_narrative(runs: tuple[AgentRun, ...]) -> str:
    return "\n".join(run.narrative for run in runs)


def _rate(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


__all__ = [
    "score_citation_validity",
    "score_groundedness",
    "score_material_fact_coverage",
    "score_numeric_provenance",
    "score_schema_conformance",
]
