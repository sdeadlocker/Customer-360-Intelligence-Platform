"""Reporting and comparison (task 11.11, design §14.6).

Turns a :class:`~c360.eval.results.RunRecord` into the two report shapes design §14.6 names -- a
JSON
report for machines and a Markdown summary for humans -- and provides the champion/challenger
comparison and the promotion regression gate.

The regression gate is the mechanism design §14.7 relies on: a challenger may only be promoted when
it does not regress the champion beyond tolerance on any *gated* dimension. Soft dimensions are
diffed and reported but a soft regression alone never blocks a promotion -- the point of the gate is
to stop a change that trades a hard guarantee (leakage, fabrication) for a quality gain, not to
freeze every metric. A hard-gate dimension that fails outright in the challenger blocks
unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from c360.eval.results import DimensionScore, RunRecord


def render_json(record: RunRecord) -> dict[str, Any]:
    """The machine-readable report: the whole record as a JSON-ready dict (design §14.6)."""
    return {
        "run_id": record.run_id,
        "created_at": record.created_at,
        "mode": record.mode,
        "provider": record.provider,
        "model_id": record.model_id,
        "prompt_versions": record.prompt_versions,
        "data_seed": record.data_seed,
        "data_count": record.data_count,
        "code_revision": record.code_revision,
        "config": record.config,
        "panel_size": record.panel_size,
        "passed": record.passed,
        "scores": [
            {
                "dimension": s.dimension,
                "name": s.name,
                "value": s.value,
                "threshold": s.threshold,
                "hard_gate": s.hard_gate,
                "passed": s.passed,
                "detail": s.detail,
            }
            for s in record.scores
        ],
    }


def render_markdown(record: RunRecord) -> str:
    """The human-readable summary: a headline verdict and a per-dimension table (design §14.6)."""
    verdict = "PASS" if record.passed else "FAIL"
    lines = [
        f"# Evaluation report -- {record.run_id}",
        "",
        f"**Verdict:** {verdict}  ",
        f"**Mode:** {record.mode} · **Provider:** {record.provider} · "
        f"**Model:** {record.model_id}  ",
        f"**Data:** seed {record.data_seed}, {record.data_count} customers, "
        f"panel {record.panel_size}  ",
        f"**Code:** {record.code_revision} · **Created:** {record.created_at}",
        "",
        "| # | Dimension | Value | Threshold | Gate | Result |",
        "|---|---|---|---|---|---|",
    ]
    for score in record.scores:
        gate = "hard" if score.hard_gate else "soft"
        result = "✓" if score.passed else "✗"
        lines.append(
            f"| {score.dimension} | {score.name} | {_fmt(score.value)} | "
            f"{_fmt(score.threshold)} | {gate} | {result} |"
        )

    failures = record.hard_gate_failures()
    if failures:
        lines.extend(["", "## Hard-gate failures", ""])
        lines.extend(f"- **{f.name}** (dimension {f.dimension}): {_fmt(f.value)}" for f in failures)
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class DimensionDelta:
    """The change in one dimension between a baseline and a candidate run."""

    dimension: int
    name: str
    baseline: float | None
    candidate: float | None
    delta: float | None
    hard_gate: bool
    #: True when the candidate regressed this dimension beyond tolerance (lower is worse).
    regressed: bool


def diff_runs(
    baseline: RunRecord, candidate: RunRecord, *, tolerance_pts: float
) -> list[DimensionDelta]:
    """Per-dimension delta between two runs, flagging regressions (design §14.6).

    ``tolerance_pts`` is a percentage-point slack (design §18 ``eval_regression_tolerance_pts``): a
    candidate is "regressed" on a dimension when its value drops more than that slack below the
    baseline. Metrics where lower is better (a leak count, a latency) are handled by their scorer
    reporting a value where higher is better is *not* assumed -- the two count-style dimensions
    (entitlement leaks, refusal false-answers) are compared as "did the count rise", the rest as
    "did the rate fall".
    """
    tolerance = tolerance_pts / 100.0
    baseline_by_dim = {s.dimension: s for s in baseline.scores}
    deltas: list[DimensionDelta] = []
    for candidate_score in sorted(candidate.scores, key=lambda s: s.dimension):
        base = baseline_by_dim.get(candidate_score.dimension)
        deltas.append(_delta(base, candidate_score, tolerance))
    return deltas


def regression_check(
    baseline: RunRecord, candidate: RunRecord, *, tolerance_pts: float
) -> tuple[bool, list[DimensionDelta]]:
    """Whether ``candidate`` may be promoted over ``baseline`` (design §14.6, §14.7).

    Promotion is blocked when the candidate fails any hard gate outright, or regresses any *gated*
    dimension beyond tolerance. Returns ``(ok, blocking_deltas)`` -- ``ok`` is true when nothing
    blocks; ``blocking_deltas`` names what did. A soft-dimension regression is reported by
    :func:`diff_runs` but never blocks here.
    """
    if not candidate.passed:
        # A challenger that does not itself pass its hard gates can never be promoted.
        failing = [
            DimensionDelta(
                dimension=f.dimension,
                name=f.name,
                baseline=None,
                candidate=f.value,
                delta=None,
                hard_gate=True,
                regressed=True,
            )
            for f in candidate.hard_gate_failures()
        ]
        return False, failing

    blocking = [
        delta
        for delta in diff_runs(baseline, candidate, tolerance_pts=tolerance_pts)
        if delta.regressed and delta.hard_gate
    ]
    return (not blocking), blocking


# ---------------------------------------------------------------- helpers


#: Dimensions whose metric is a count where *lower is better* (a leak count, a false-answer count).
#: For these a rise is a regression; for every other dimension a fall in the rate is a regression.
_LOWER_IS_BETTER = frozenset({6, 13, 14, 15})


def _delta(
    baseline: DimensionScore | None, candidate: DimensionScore, tolerance: float
) -> DimensionDelta:
    base_value = None if baseline is None else baseline.value
    cand_value = candidate.value
    delta = None
    regressed = False
    if base_value is not None and cand_value is not None:
        delta = cand_value - base_value
        if candidate.dimension in _LOWER_IS_BETTER:
            regressed = delta > tolerance
        else:
            regressed = delta < -tolerance
    return DimensionDelta(
        dimension=candidate.dimension,
        name=candidate.name,
        baseline=base_value,
        candidate=cand_value,
        delta=delta,
        hard_gate=candidate.hard_gate,
        regressed=regressed,
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return "--"
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}"


def render_comparison_markdown(
    baseline: RunRecord, candidate: RunRecord, deltas: list[DimensionDelta], *, promotable: bool
) -> str:
    """A human-readable champion-vs-challenger table with the promotion verdict."""
    verdict = "PROMOTABLE" if promotable else "BLOCKED"
    lines = [
        f"# Champion vs challenger -- {verdict}",
        "",
        f"**Champion:** {baseline.run_id} ({baseline.model_id})  ",
        f"**Challenger:** {candidate.run_id} ({candidate.model_id})",
        "",
        "| # | Dimension | Champion | Challenger | Δ | Gate | Regressed |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in deltas:
        gate = "hard" if d.hard_gate else "soft"
        flag = "⚠️" if d.regressed else ""
        lines.append(
            f"| {d.dimension} | {d.name} | {_fmt(d.baseline)} | {_fmt(d.candidate)} | "
            f"{_fmt(d.delta)} | {gate} | {flag} |"
        )
    return "\n".join(lines)


__all__ = [
    "DimensionDelta",
    "diff_runs",
    "regression_check",
    "render_comparison_markdown",
    "render_json",
    "render_markdown",
]
