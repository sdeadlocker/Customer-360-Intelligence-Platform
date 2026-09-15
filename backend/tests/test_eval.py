"""Phase 11 evaluation framework tests (task 11.12 gate, design §14).

These prove the framework does what the phase gate requires: the deterministic hard gates pass on a
clean mock run, and a deliberately introduced regression fails. The expensive end-to-end pieces (a
full :func:`run_evaluation`, the entitlement and adversarial passes) run on a small panel so the
suite stays inside the fast inner loop, while the pure scorers, the ground-truth export, the store
and the report/comparison logic are exercised directly with hand-built fixtures.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from c360.agents.result import AgentResult
from c360.agents.schemas import FinancialHealthOutput, RiskOutput
from c360.eval.adversarial import run_adversarial_suite, score_adversarial
from c360.eval.ground_truth import export_ground_truth
from c360.eval.harness import AgentRun, EvalHarness, HarnessOutput
from c360.eval.principals import role_principals
from c360.eval.question_bank import BehaviorClass, build_question_bank
from c360.eval.report import diff_runs, regression_check, render_json, render_markdown
from c360.eval.results import DimensionScore, RunRecord
from c360.eval.retrieval_labels import retrieval_labels
from c360.eval.runner import EvalMode, run_evaluation
from c360.eval.scorers import deterministic
from c360.eval.scorers.entitlement import score_entitlement_safety
from c360.eval.scorers.qa import score_qa
from c360.eval.scorers.retrieval import score_retrieval
from c360.eval.store import EvalStore
from c360.security.model import Role
from c360.tools.facts import FactTable
from tests.conftest import make_settings

# A small panel keeps the integration tests fast while still covering every cohort.
_EVAL_COUNT = 100
_EVAL_PANEL = 1


# ---------------------------------------------------------------- fixtures


def _fact_table(field: str, value: object) -> FactTable:
    builder = FactTable.builder()
    builder.add(entity_type="financial_profile", entity_id="C-1", field=field, value=value)
    return builder.build()


def _agent_run(agent: str, narrative: str, facts: FactTable) -> AgentRun:
    outputs: BaseModel
    if agent == "risk":
        outputs = RiskOutput(narrative=narrative)
    else:
        outputs = FinancialHealthOutput(narrative=narrative)
    result = AgentResult(
        agent=agent,
        outputs=outputs,
        generated_at=datetime.now(tz=UTC),
        model_id="mock-llm-v1",
        prompt_version="test",
    )
    return AgentRun(customer_id="C-1", agent=agent, result=result, facts=facts)


@pytest.fixture(scope="module")
def ground_truth() -> object:
    return export_ground_truth(count=_EVAL_COUNT, seed=42, panel_per_cohort=_EVAL_PANEL)


# ---------------------------------------------------------------- ground truth (11.1)


def test_ground_truth_panel_covers_every_cohort(ground_truth: object) -> None:
    counts = ground_truth.cohort_counts()  # type: ignore[attr-defined]
    # All eight cohorts present, each at least the requested floor.
    assert len(counts) == 8
    assert all(count >= _EVAL_PANEL for count in counts.values())


def test_ground_truth_fraud_cohort_has_material_facts(ground_truth: object) -> None:
    # At least one fraud-flagged customer in the panel carries the AML/PEP material fact.
    kinds = {
        fact.kind
        for customer in ground_truth.by_customer.values()  # type: ignore[attr-defined]
        for fact in customer.material_facts
    }
    assert "AML_PEP" in kinds


def test_retrieval_labels_cover_all_domains() -> None:
    labels = retrieval_labels()
    domains = {label.domain.value for label in labels}
    assert len(domains) == 6
    assert len(labels) >= 20


# ---------------------------------------------------------------- store (11.2)


def test_store_round_trips_a_run(tmp_path: Path) -> None:
    store = EvalStore(tmp_path / "eval.db")
    record = RunRecord(
        run_id="r1",
        created_at="2026-09-13T00:00:00Z",
        mode="ci",
        provider="mock",
        model_id="mock-llm-v1",
        prompt_versions={"summary": "v1"},
        data_seed=42,
        data_count=100,
        code_revision="abc",
        config={"mode": "ci"},
        panel_size=24,
        passed=True,
        scores=(DimensionScore(1, "Schema", 1.0, 1.0, True, True, {}),),
    )
    store.save_run(record)
    store.set_baseline("r1")
    assert store.get_run("r1").run_id == "r1"  # type: ignore[union-attr]
    assert store.baseline_run().run_id == "r1"  # type: ignore[union-attr]
    assert store.latest_run().run_id == "r1"  # type: ignore[union-attr]


# ---------------------------------------------------------------- deterministic scorers (11.3)


def test_groundedness_passes_on_cited_narrative() -> None:
    facts = _fact_table("net_worth_cents", 3675122)
    run = _agent_run("financial_health", "Net worth is 3675122 [F1].", facts)
    output = HarnessOutput(agent_runs=(run,))
    assert deterministic.score_groundedness(output).passed


def test_groundedness_fails_on_fabricated_figure() -> None:
    facts = _fact_table("net_worth_cents", 3675122)
    # An uncited figure that matches nothing — a fabrication.
    run = _agent_run("financial_health", "Net worth is 9999999 with no citation.", facts)
    output = HarnessOutput(agent_runs=(run,))
    score = deterministic.score_groundedness(output)
    assert not score.passed
    assert score.hard_gate


def test_numeric_provenance_fails_on_passage_sourced_figure() -> None:
    facts = _fact_table("net_worth_cents", 3675122)
    run = _agent_run("financial_health", "The rate is 500 [P1].", facts)
    output = HarnessOutput(agent_runs=(run,))
    assert not deterministic.score_numeric_provenance(output).passed


# ---------------------------------------------------------------- reporting / comparison (11.11)


def _run(run_id: str, groundedness: float) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        created_at="2026-09-13T00:00:00Z",
        mode="ci",
        provider="mock",
        model_id="mock-llm-v1",
        prompt_versions={},
        data_seed=42,
        data_count=100,
        code_revision="abc",
        config={},
        panel_size=24,
        passed=groundedness >= 1.0,
        scores=(
            DimensionScore(2, "Groundedness", groundedness, 1.0, True, groundedness >= 1.0, {}),
        ),
    )


def test_report_renders_json_and_markdown() -> None:
    record = _run("r1", 1.0)
    assert render_json(record)["run_id"] == "r1"
    assert "Evaluation report" in render_markdown(record)


def test_regression_blocks_a_hard_gate_drop() -> None:
    baseline = _run("champion", 1.0)
    challenger = _run("challenger", 0.5)  # groundedness fell below the hard gate
    promotable, blocking = regression_check(baseline, challenger, tolerance_pts=1.0)
    assert not promotable
    assert blocking


def test_no_regression_allows_promotion() -> None:
    baseline = _run("champion", 1.0)
    challenger = _run("challenger", 1.0)
    promotable, blocking = regression_check(baseline, challenger, tolerance_pts=1.0)
    assert promotable
    assert not blocking
    # And the diff surfaces every dimension.
    assert diff_runs(baseline, challenger, tolerance_pts=1.0)


# ---------------------------------------------------------------- question bank (11.7)


def test_question_bank_has_every_behavior_class(ground_truth: object) -> None:
    bank = build_question_bank(ground_truth)  # type: ignore[arg-type]
    classes = {q.behavior for q in bank}
    assert BehaviorClass.ANSWER in classes
    assert BehaviorClass.DENY_ENTITLEMENT in classes


def test_role_principals_cover_every_role() -> None:
    principals = role_principals()
    assert set(principals) == set(Role)


# ---------------------------------------------------------------- integration (11.4, 11.5, 11.13)
#
# These build their own harness and run every async step inside one ``asyncio.run``. The Q&A
# checkpointer binds an aiosqlite connection to the loop it is created on, so all Q&A work for a
# harness must happen on a single loop and the connection be closed on it -- hence one async
# body per test rather than a shared harness across several ``asyncio.run`` calls.


def _make_harness(ground_truth: object, workdir: Path) -> EvalHarness:
    harness = EvalHarness(make_settings(), ground_truth, workdir=workdir)  # type: ignore[arg-type]
    harness.prepare()
    return harness


@pytest.mark.slow
def test_entitlement_and_adversarial_hold(ground_truth: object, tmp_path: Path) -> None:
    """Dimensions 6 and 7 -- the zero-tolerance security gates -- hold on a clean mock run."""

    async def _run() -> tuple[object, object]:
        harness = _make_harness(ground_truth, tmp_path / "ent")
        try:
            entitlement = await score_entitlement_safety(harness)
            adversarial = score_adversarial(await run_adversarial_suite(harness, ground_truth))  # type: ignore[arg-type]
            return entitlement, adversarial
        finally:
            await harness.aclose_qa()
            harness.dispose()

    entitlement, adversarial = asyncio.run(_run())
    assert entitlement.passed  # type: ignore[attr-defined]
    assert entitlement.value == 0.0  # type: ignore[attr-defined]
    assert adversarial.passed  # type: ignore[attr-defined]
    assert adversarial.value == 1.0  # type: ignore[attr-defined]


@pytest.mark.slow
def test_qa_and_retrieval(ground_truth: object, tmp_path: Path) -> None:
    """Q&A never misclassifies a denial (dim 11) and retrieval attribution is correct (dim 12)."""

    async def _run() -> tuple[object, object]:
        harness = _make_harness(ground_truth, tmp_path / "qa")
        try:
            dim11, _dim13 = await score_qa(harness, ground_truth, limit=8)  # type: ignore[arg-type]
            retrieval = score_retrieval(harness, recall_floor=0.85, full_mode=False)
            return dim11, retrieval
        finally:
            await harness.aclose_qa()
            harness.dispose()

    dim11, retrieval = asyncio.run(_run())
    assert dim11.detail["misclassified_denials"] == 0  # type: ignore[attr-defined]
    assert dim11.passed  # type: ignore[attr-defined]
    if retrieval.detail.get("status") != "unavailable":  # type: ignore[attr-defined]
        assert retrieval.detail["attribution_correctness"] == 1.0  # type: ignore[attr-defined]
    assert retrieval.passed  # type: ignore[attr-defined]


@pytest.mark.slow
def test_ci_run_passes_hard_gates_and_regression_fails() -> None:
    """The phase gate: a clean CI run passes; a fabricated-figure regression fails (§14.8)."""
    settings = make_settings(seed_customer_count=_EVAL_COUNT, eval_panel_per_cohort=_EVAL_PANEL)
    record = asyncio.run(run_evaluation(settings, EvalMode.CI))
    assert record.passed, record.hard_gate_failures()
    # Every hard gate resolved to a pass.
    assert all(score.passed for score in record.scores if score.hard_gate)

    # A deliberately introduced regression -- a fabricated figure -- fails the groundedness gate.
    facts = _fact_table("net_worth_cents", 100)
    bad = _agent_run("financial_health", "Net worth is 424242 with no citation.", facts)
    regressed = deterministic.score_groundedness(HarnessOutput(agent_runs=(bad,)))
    assert not regressed.passed
