"""The evaluation run orchestrator (tasks 11.11, 11.12; design §14.5, §14.6).

Ties the whole framework together: export the ground truth, seed a panel database, run the agents
and
Q&A over it through the real entry points, apply every scorer, and assemble the sixteen dimension
scores into a :class:`~c360.eval.results.RunRecord` with the attribution header design §14.6
requires
(prompt versions, model id, provider, data seed, code revision, config).

Two modes, per design §14.5:

* ``ci`` -- the mock provider, a small Q&A subset, retrieval scored in advisory mode. Fast,
  deterministic, free; runs on every change and enforces the hard gates that catch the failures that
  matter most -- leakage, fabrication, injection (design §14.5).
* ``full`` -- the live provider, the full Q&A bank, retrieval recall gated. Runs nightly and
  pre-release with cost reporting.

The run's ``passed`` verdict is exactly "no hard-gate dimension failed" -- the soft dimensions are
reported and diffed but never block. This is what makes the framework a gate rather than ceremony:
the five zero-tolerance dimensions (schema, groundedness, provenance, entitlement, adversarial) plus
the deterministic denial gate are the wall, and everything else is a dashboard.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from c360.eval.adversarial import run_adversarial_suite, score_adversarial
from c360.eval.ground_truth import GroundTruth, export_ground_truth
from c360.eval.harness import EvalHarness
from c360.eval.results import DimensionScore, RunRecord
from c360.eval.scorers import deterministic, quality
from c360.eval.scorers.entitlement import score_entitlement_safety
from c360.eval.scorers.judge import score_qualitative
from c360.eval.scorers.performance import score_latency_cost, score_stability
from c360.eval.scorers.qa import score_qa
from c360.eval.scorers.retrieval import score_retrieval

if TYPE_CHECKING:
    from c360.core.config import Settings

#: The Q&A subset size for CI. Small enough to be fast, large enough (via the balanced prefix) to
#: exercise every behaviour class including the deterministic denial gate.
_CI_QA_LIMIT = 12


class EvalMode(StrEnum):
    """Execution modes of design §14.5."""

    CI = "ci"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The knobs a run resolves from settings and mode, for the record's config blob."""

    mode: EvalMode
    count: int
    seed: int
    panel_per_cohort: int
    qa_limit: int | None
    full_retrieval_gate: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "count": self.count,
            "seed": self.seed,
            "panel_per_cohort": self.panel_per_cohort,
            "qa_limit": self.qa_limit,
            "full_retrieval_gate": self.full_retrieval_gate,
        }


def _resolve_config(settings: Settings, mode: EvalMode) -> RunConfig:
    """Resolve the run knobs from settings and mode (design §14.5)."""
    full = mode is EvalMode.FULL
    return RunConfig(
        mode=mode,
        count=settings.seed_customer_count,
        seed=settings.seed_random_seed,
        panel_per_cohort=settings.eval_panel_per_cohort,
        qa_limit=None if full else _CI_QA_LIMIT,
        full_retrieval_gate=full,
    )


async def run_evaluation(
    settings: Settings, mode: EvalMode, *, workdir: Path | None = None
) -> RunRecord:
    """Run the whole evaluation and return the assembled, unsaved :class:`RunRecord`.

    ``workdir`` is where the throwaway panel database lives; a temp directory is created and cleaned
    up when none is given. Persisting the record is the caller's job (the CLI saves it), so a test
    can
    run an evaluation without touching ``eval.db``.
    """
    settings = _apply_mode_provider(settings, mode)
    config = _resolve_config(settings, mode)
    ground_truth = export_ground_truth(
        count=config.count, seed=config.seed, panel_per_cohort=config.panel_per_cohort
    )

    if workdir is not None:
        return await _run_in(settings, mode, config, ground_truth, workdir)
    with tempfile.TemporaryDirectory(prefix="c360-eval-") as tmp:
        return await _run_in(settings, mode, config, ground_truth, Path(tmp))


def _apply_mode_provider(settings: Settings, mode: EvalMode) -> Settings:
    """Force the provider the mode requires, regardless of ambient config (design §14.5).

    ``ci`` must be deterministic and free on any machine, so it pins the mock provider even if the
    developer's environment is configured for Bedrock. ``full`` uses what is configured -- a
    scheduled run against a real model, so a misconfigured Bedrock is a fail-fast the operator
    wants,
    not something to paper over. Overriding only the CI direction keeps full mode honest.
    """
    from c360.core.config import LlmProvider  # noqa: PLC0415

    if mode is EvalMode.CI and settings.llm_provider is not LlmProvider.MOCK:
        return settings.model_copy(update={"llm_provider": LlmProvider.MOCK})
    return settings


async def _run_in(
    settings: Settings,
    mode: EvalMode,
    config: RunConfig,
    ground_truth: GroundTruth,
    workdir: Path,
) -> RunRecord:
    harness = EvalHarness(settings, ground_truth, workdir=workdir)
    harness.prepare()
    try:
        # Capture the attribution metadata while the harness is live; disposal frees the runtime.
        model_id = harness.model_id
        prompt_versions = harness.prompt_versions()
        scores = await _score_all(harness, ground_truth, config, settings)
    finally:
        await harness.aclose_qa()
        harness.dispose()

    passed = all(score.passed for score in scores if score.hard_gate)
    return RunRecord(
        run_id=_run_id(mode),
        created_at=datetime.now(tz=UTC).isoformat(),
        mode=mode.value,
        provider=str(settings.llm_provider),
        model_id=model_id,
        prompt_versions=prompt_versions,
        data_seed=config.seed,
        data_count=config.count,
        code_revision=_code_revision(),
        config=config.as_dict(),
        panel_size=len(ground_truth.panel),
        passed=passed,
        scores=tuple(scores),
    )


async def _score_all(
    harness: EvalHarness,
    ground_truth: GroundTruth,
    config: RunConfig,
    settings: Settings,
) -> list[DimensionScore]:
    """Run every dimension's scorer and return the scores in dimension order.

    The dashboard is run once and its output feeds the five deterministic scorers, the three task
    scorers and the qualitative proxy; the entitlement, adversarial, Q&A, retrieval and performance
    scorers drive their own passes because they need per-role, per-question or repeated runs.
    """
    output = await harness.run_dashboard()

    scores: list[DimensionScore] = [
        deterministic.score_schema_conformance(output),
        deterministic.score_groundedness(output),
        deterministic.score_numeric_provenance(output),
        deterministic.score_citation_validity(output),
        deterministic.score_material_fact_coverage(output, ground_truth),
        await score_entitlement_safety(harness),
        score_adversarial(await run_adversarial_suite(harness, ground_truth)),
        quality.score_life_event_detection(output, ground_truth),
        quality.score_offer_ranking(output, ground_truth),
        quality.score_risk_driver_correctness(output, ground_truth),
    ]

    dim11, dim13 = await score_qa(harness, ground_truth, limit=config.qa_limit)
    scores.append(dim11)  # 11
    scores.append(
        score_retrieval(
            harness,
            recall_floor=settings.eval_retrieval_recall_min,
            full_mode=config.full_retrieval_gate,
        )
    )  # 12
    scores.append(dim13)  # 13
    scores.append(await score_latency_cost(harness, settings.cost_price_table))  # 14
    scores.append(await score_stability(harness))  # 15
    scores.append(score_qualitative(output))  # 16

    scores.sort(key=lambda score: score.dimension)
    return scores


# ---------------------------------------------------------------- attribution helpers


def _run_id(mode: EvalMode) -> str:
    """A sortable, human-readable run id: mode plus a UTC timestamp to the microsecond."""
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{mode.value}-{stamp}"


def _code_revision() -> str:
    """The current git commit, or ``"unknown"`` outside a repository (design §14.6).

    Recorded because the generator's draw order depends on code (design §15), so two runs at the
    same
    seed can differ if the generator changed -- the revision makes that attributable. Best-effort: a
    missing git or a non-repo checkout yields ``"unknown"`` rather than failing the run.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git resolved from PATH
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = result.stdout.strip()
    return revision or "unknown"


__all__ = ["EvalMode", "RunConfig", "run_evaluation"]
