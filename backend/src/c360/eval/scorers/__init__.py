"""The evaluation scorers (tasks 11.3-11.10, design §14.3).

Each scorer turns harness output (and, where the truth is known, the ground truth) into one
:class:`~c360.eval.results.DimensionScore` of design §14.3. Most are *deterministic assertions*
against the generator's exported labels rather than a model judging a model (design §14.1) -- which
is
what lets the hard gates run for free in CI with the mock provider.

The dimensions split across modules by concern: :mod:`deterministic` covers schema conformance,
groundedness, numeric provenance, citation validity and material-fact coverage (dimensions 1-5);
:mod:`entitlement` covers dimension 6; the adversarial resistance scorer (dimension 7) lives with
the suite in :mod:`c360.eval.adversarial`; :mod:`quality` covers life-event, offer and risk-driver
correctness (dimensions 8-10); :mod:`qa` covers Q&A correctness and refusal calibration (11, 13);
:mod:`retrieval` covers retrieval quality (12); :mod:`performance` covers latency and cost
(14, 15); and :mod:`judge` covers the advisory qualitative dimension (16).
"""

from __future__ import annotations
