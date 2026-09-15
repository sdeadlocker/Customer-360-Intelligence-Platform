"""Proactive alerting and the signals feed (Phase 17).

The platform up to Phase 16 is pull-only: a relationship manager opens a customer and reads what the
deterministic layer already computed. This package flips that to push. It surfaces the signals the
platform *already* derives — a risk band moving up, a large deposit opening a cross-sell window, a
detected life event, an AML/PEP flag — as a ranked, entitlement-scoped worklist an RM can triage.

Datastore rule (design §4.2, task 17.1)
---------------------------------------

Signals are **derived** from the read-only ``customer.db`` and its derived tables; nothing here
writes a customer-database row. Signal instances and their per-user state live in the separate,
writable ``signals.db`` (:mod:`c360.signals.store`), created and migrated exactly the way the other
writable stores (``audit.db``, ``checkpoints.db``) are. ``customer.db`` and its schema are never
changed — task 17.7's read-only proof asserts a write attempt against it raises.

Layering
--------

* :mod:`c360.signals.models` — the typed signal, its evidence and state value types.
* :mod:`c360.signals.store` — the writable ``signals.db`` schema, its migration head and the
  single-writer upsert path.
* :mod:`c360.signals.detectors` — deterministic detectors over the existing services; no LLM.
* :mod:`c360.signals.ranking` — severity x value-at-stake x recency prioritization.
* :mod:`c360.signals.detect` — the idempotent batch job the CLI and admin endpoint run.
* :mod:`c360.signals.repository` — read-only reads of ``signals.db`` for the API.
* :mod:`c360.signals.service` — the entitlement-scoped worklist read model.
"""

from __future__ import annotations
