"""Metric label allowlist (task 10.3, requirement 18.9, design §13.4).

The span-attribute allowlist (``allowlist.py``) protects *traces*. Metrics need the same bright
line, for a sharper reason: a metric label is a dimension, and an unbounded dimension is both a
cardinality incident that melts the time-series database and a privacy incident when the unbounded
value is a ``customer_id``.

Design §13.4 fixes the permitted labels: ``role``, ``segment``, ``route``, ``agent``, ``model``,
``outcome``, ``domain`` — every one low-cardinality. This module turns that table into code so a
new instrument cannot quietly attach ``customer_id`` or a raw balance. The allowlist is applied by
:func:`scrub_labels`, which every metric-emitting helper in :mod:`c360.core.telemetry.metrics`
routes its attributes through before ``add``/``record``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: The label keys design §13.4 permits on any exported metric. Anything not here is dropped before
#: it can reach an instrument. Keys are the short GenAI/semconv names plus the retrieval ``stage``
#: dimension the knowledge layer already uses; the ``c360.*`` retrieval keys are grandfathered so
#: the Phase 7 instruments keep working unchanged.
ALLOWED_METRIC_LABELS: Final[frozenset[str]] = frozenset(
    {
        # design §13.4 — the seven low-cardinality dimensions
        "role",
        "segment",
        "route",
        "agent",
        "model",
        "outcome",
        "domain",
        # HTTP RED needs method and status alongside route; both are bounded.
        "http.request.method",
        "http.response.status_code",
        # retrieval/db/graph/security dimensions, all bounded
        "stage",
        "reranked",
        "reason",
        "statement_id",
        "field_group",
        "breaker",
        "state",
        # signals feed (Phase 17): all bounded closed sets — four types, three severities, four
        # per-user states.
        "signal_type",
        "severity",
        "signal_status",
        # report exports (Phase 18): a bounded closed set of two report types; `outcome` (above)
        # carries the run's success/failure. No customer id, book or title is ever a label.
        "report_type",
        "c360.retrieval.stage",  # grandfathered Phase 7 label
    }
)


def label_permitted(key: str) -> bool:
    """Whether ``key`` may be used as a metric label."""
    return key in ALLOWED_METRIC_LABELS


def scrub_labels(labels: Mapping[str, object] | None) -> dict[str, str]:
    """Return only the permitted labels, coerced to strings.

    Metric attribute values are coerced to ``str`` so a boolean or int cannot silently create two
    representations of the same series. A dropped key is dropped silently — the caller should not
    have supplied it, and raising would turn a telemetry mistake into a request failure.
    """
    if not labels:
        return {}
    return {key: str(value) for key, value in labels.items() if label_permitted(key)}


def rejected_labels(labels: Mapping[str, object] | None) -> tuple[str, ...]:
    """Return the label keys that would be dropped. Used by the leakage test and diagnostics."""
    if not labels:
        return ()
    return tuple(key for key in labels if not label_permitted(key))


__all__ = [
    "ALLOWED_METRIC_LABELS",
    "label_permitted",
    "rejected_labels",
    "scrub_labels",
]
