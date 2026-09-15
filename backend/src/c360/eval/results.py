"""The value types an evaluation run produces and the store persists (task 11.2, design §14.3).

A run is a :class:`RunRecord`: an attribution header (design §14.6 -- prompt versions, model id,
provider, data seed, code revision, config) plus one :class:`DimensionScore` per scored dimension of
design §14.3. A dimension score carries its numeric value, the gate threshold, whether it is a hard
gate, whether it passed, and a JSON ``detail`` blob for the report (per-agent breakdowns, confusion
matrices, offending payloads). Keeping ``detail`` free-form JSON rather than a typed column per
dimension is deliberate: the sixteen dimensions have wildly different shapes, and the report renders
them generically.

These types are pure data with no I/O, so a scorer builds a :class:`DimensionScore` and the runner
assembles a :class:`RunRecord` without either touching the database -- :mod:`c360.eval.store` is the
only module that knows SQLite exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DimensionScore:
    """One scored dimension of design §14.3.

    ``value`` is the headline metric (a rate in 0..1, a count, a latency); ``threshold`` is the gate
    it is compared against, or ``None`` for an advisory dimension with no gate. ``hard_gate`` marks
    the five zero/hundred-tolerance dimensions (1, 2, 3, 6, 7) whose failure fails the whole run.
    ``passed`` is the resolved verdict -- an advisory dimension always "passes" so it never blocks.
    """

    dimension: int
    name: str
    value: float | None
    threshold: float | None
    hard_gate: bool
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A whole evaluation run: attribution header plus per-dimension scores (design §14.6)."""

    run_id: str
    created_at: str
    mode: str
    provider: str
    model_id: str
    #: Agent name → prompt version (``"summary": "v3+ab12cd..."``). Every prompt the run exercised.
    prompt_versions: dict[str, str]
    data_seed: int
    data_count: int
    #: A short identifier of the code the run executed against (git sha or "unknown"). Design §14.6
    #: lists it among the attributes that make a quality change attributable; the generator's draw
    #: order depends on code (design §15 note), so the label is recorded even though it is advisory.
    code_revision: str
    config: dict[str, Any]
    panel_size: int
    passed: bool
    scores: tuple[DimensionScore, ...] = ()

    def score_for(self, dimension: int) -> DimensionScore | None:
        """The score for ``dimension``, or ``None`` if this run did not measure it."""
        for score in self.scores:
            if score.dimension == dimension:
                return score
        return None

    def hard_gate_failures(self) -> tuple[DimensionScore, ...]:
        """The hard-gate dimensions that failed -- the reasons a run is blocked."""
        return tuple(s for s in self.scores if s.hard_gate and not s.passed)


__all__ = ["DimensionScore", "RunRecord"]
