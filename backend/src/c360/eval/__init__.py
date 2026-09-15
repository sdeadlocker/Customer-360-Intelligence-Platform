"""The agent evaluation framework (Phase 11, design §14).

Ground truth for this platform is not estimated -- it is *known*. The dataset comes from a
deterministic seeded generator (design §15), so the generator can export the true answers as labels:
which life events it created, which customers it made delinquent, each customer's true acceptance
propensity, and which factors composed each risk score (design §14.1). Because those labels are
written as data during generation and never recomputed at eval time, a change to service code cannot
silently move the ground truth it is being measured against (design §15.1).

The package layers into: ground-truth export (:mod:`c360.eval.ground_truth`), the ``eval.db``
datastore (:mod:`c360.eval.store`), the harness that runs agents and Q&A over the panel through the
same entry points the API uses (:mod:`c360.eval.harness`), the scorers (:mod:`c360.eval.scorers`),
the adversarial suite (:mod:`c360.eval.adversarial`), the Q&A question bank
(:mod:`c360.eval.question_bank`), reporting and champion/challenger comparison
(:mod:`c360.eval.report`), and the run orchestrator (:mod:`c360.eval.runner`).
"""

from __future__ import annotations
