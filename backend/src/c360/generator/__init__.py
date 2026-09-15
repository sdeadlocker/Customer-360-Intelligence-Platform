"""Seeded synthetic data generator (Phase 2, design §15).

Produces the customer database the whole platform is developed and evaluated against. Two properties
matter more than realism:

**Reproducibility.** The same ``--count`` and ``--seed`` produce a byte-identical database
(requirement 14.7), which is what allows the evaluation framework of Phase 11 to attribute a quality
change to a prompt or a model rather than to the data having moved underneath it.

**Coverage of states, not of volume.** Generation is driven by the persona distribution in design
§15
rather than by uniform randomness, with a per-cohort floor so no persona rounds away at small
dataset
sizes. That is what makes the delinquency banner, the AML indicator, the thin-file empty states and
the
no-relationships graph reachable in a hundred-customer development dataset.

Entry points
------------

:func:`c360.generator.pipeline.seed` migrates, generates and loads.
:func:`c360.generator.pipeline.generate`
stops before the filesystem, returning the plan and the staged rows, which is what tests and the
Phase 11
ground-truth export use.

Module map
----------

===============  ============================================================
``cohorts``      the eight personas and the population split
``context``      the single seeded ``Random`` and the drawing helpers
``vocab``        name, place, product and merchant vocabularies
``plan``         the in-memory plan every stage reads and extends
``people``       identity, contact details, household formation
``products``     accounts, holdings and linked assets
``milestones``   life-event planning
``transactions`` the transaction stream and life-event corroboration
``network``      household membership, relationships, parties, beneficiaries
``events``       applications, campaigns, offers, engagement, life-event rows
``profiles``     the derived financial, credit and risk profiles
``adversarial``  instruction-like payloads for the Phase 11 red-team suite
``emit``         row emission for the identity, product and asset tables
``tables``       table declarations and load order
``loader``       the batched single-transaction load
``pipeline``     stage order and the seeding entry point
===============  ============================================================
"""

from __future__ import annotations

from c360.generator.cohorts import COHORT_PROFILES, Cohort, allocate
from c360.generator.context import DEFAULT_AS_OF, HISTORY_MONTHS, GeneratorContext
from c360.generator.pipeline import SeedReport, generate, seed
from c360.generator.tables import LOAD_ORDER, Dataset

__all__ = [
    "COHORT_PROFILES",
    "DEFAULT_AS_OF",
    "HISTORY_MONTHS",
    "LOAD_ORDER",
    "Cohort",
    "Dataset",
    "GeneratorContext",
    "SeedReport",
    "allocate",
    "generate",
    "seed",
]
