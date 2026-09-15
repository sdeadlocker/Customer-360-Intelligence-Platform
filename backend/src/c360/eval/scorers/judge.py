"""Advisory qualitative scoring -- dimension 16 (task 11.10, design §14.3).

Design §14.3 makes this the one dimension a model judges rather than a deterministic assertion, and
it makes it **advisory only**: a model-as-judge score can never gate a build (design §19.12 risk --
"judge scores are advisory only and cannot gate a build"). So this scorer always reports
``passed=True`` regardless of the score, and the runner never counts it toward the verdict.

Two products:

* A **rubric score** for clarity, usefulness and tone. When a real generation provider is supplied,
  each narrative can be judged against the published rubric (:data:`RUBRIC`); with the mock provider
  -- which is not a judge -- a deterministic *proxy* is used instead: a transparent heuristic over
  structure (does the card have a narrative, does it cite its figures, is it a reasonable length).
  The proxy is labelled as such so nobody mistakes it for a model's judgement.
* A **10% human-review sample** -- a deterministic slice of the run's narratives exported in the
  detail so a human can spot-check quality. Deterministic (every tenth run in a stable order) so the
  same sample is reviewable across runs rather than a fresh random draw each time.

The rubric is published here as data so the judge prompt and the human reviewer read the same
criteria, which is what makes a qualitative score comparable across runs (design §14.7's argument
for
versioned, evidence-based comparison applied to the soft dimension).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from c360.eval.results import DimensionScore

if TYPE_CHECKING:
    from c360.eval.harness import AgentRun, HarnessOutput

#: The published qualitative rubric (design §14.3). Each criterion is scored 0..1; the headline is
#: their mean. Kept as data so a model judge and a human reviewer apply the identical criteria.
RUBRIC: Final[dict[str, str]] = {
    "clarity": "Is the narrative readable, unambiguous and free of jargon a banker would not use?",
    "usefulness": "Does it surface something actionable a relationship manager could act on?",
    "tone": "Is it professional, factual and free of hype or unwarranted certainty?",
}

#: Fraction of narratives exported for human review (design §14.3: "10% human review sample").
_HUMAN_REVIEW_FRACTION: Final = 0.10

#: A citation marker, used by the proxy to reward a card that grounds its figures.
_CITATION = re.compile(r"\[[FP]\d+\]")

#: Reasonable narrative length window (characters) for the clarity proxy: long enough to be useful,
#: short enough not to ramble. Outside it, clarity is scored down but never zeroed.
_MIN_USEFUL_CHARS = 80
_MAX_CLEAR_CHARS = 2000

#: Two or more citations in a card is treated as strongly grounded for the usefulness proxy.
_STRONG_CITATION_COUNT = 2


def score_qualitative(output: HarnessOutput) -> DimensionScore:
    """Dimension 16: advisory qualitative score plus a human-review sample (never gates).

    Uses the deterministic proxy rubric over the produced narratives. ``passed`` is always ``True``
    -- this dimension cannot fail a run by design. The per-criterion means and the human-review
    sample travel in the detail.
    """
    clarity: list[float] = []
    usefulness: list[float] = []
    tone: list[float] = []
    for run in output.agent_runs:
        narrative = run.narrative
        clarity.append(_clarity_proxy(narrative))
        usefulness.append(_usefulness_proxy(run))
        tone.append(_tone_proxy(narrative))

    scores = {
        "clarity": _mean(clarity),
        "usefulness": _mean(usefulness),
        "tone": _mean(tone),
    }
    overall = _mean(list(scores.values()))
    return DimensionScore(
        dimension=16,
        name="Qualitative (advisory)",
        value=overall,
        threshold=None,
        hard_gate=False,
        passed=True,  # advisory only -- never gates (design §19.12)
        detail={
            "method": "deterministic_proxy",
            "rubric": RUBRIC,
            "criteria": scores,
            "human_review_sample": _human_review_sample(output),
            "note": "Proxy rubric; a model-as-judge score replaces this under --mode full.",
        },
    )


# ---------------------------------------------------------------- proxy rubric


def _clarity_proxy(narrative: str) -> float:
    """A structure-based clarity proxy: rewards a present, reasonably-sized narrative."""
    text = narrative.strip()
    if not text:
        return 0.0
    length = len(text)
    if length < _MIN_USEFUL_CHARS:
        return 0.5
    if length > _MAX_CLEAR_CHARS:
        return 0.7
    return 1.0


def _usefulness_proxy(run: AgentRun) -> float:
    """A usefulness proxy: a card that cites concrete facts is more actionable."""
    narrative = run.narrative
    if not narrative.strip():
        return 0.0
    cited = len(_CITATION.findall(narrative))
    if cited == 0:
        return 0.4
    return 1.0 if cited >= _STRONG_CITATION_COUNT else 0.7


def _tone_proxy(narrative: str) -> float:
    """A tone proxy: penalise hype words; a factual, hype-free narrative scores full marks."""
    lowered = narrative.lower()
    hype = ("guaranteed", "amazing", "incredible", "best-ever", "unbelievable", "revolutionary")
    hits = sum(1 for word in hype if word in lowered)
    return max(0.0, 1.0 - 0.25 * hits)


def _human_review_sample(output: HarnessOutput) -> list[dict[str, str]]:
    """A deterministic 10% slice of narratives for human spot-checking (design §14.3).

    Every tenth agent run in bank order, so the same slice is reviewable across runs and a reviewer
    can compare the same cards between a champion and a challenger.
    """
    runs = output.agent_runs
    if not runs:
        return []
    step = max(1, round(1 / _HUMAN_REVIEW_FRACTION))
    return [
        {
            "customer_id": run.customer_id,
            "agent": run.agent,
            "narrative": run.narrative,
            "degraded": str(run.result.degraded),
        }
        for index, run in enumerate(runs)
        if index % step == 0
    ]


def _mean(values: list[float]) -> float:
    return 0.0 if not values else sum(values) / len(values)


__all__ = ["RUBRIC", "score_qualitative"]
