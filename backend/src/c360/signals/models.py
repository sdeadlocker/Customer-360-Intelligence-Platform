"""Signal value types (task 17.1, 17.2).

A *signal* is one derived, inspectable alert about one customer: what kind it is, how severe, how
much is at stake, and the evidence — with fact citations — that produced it. These types are the
shape written into ``signals.db`` and read back for the worklist; they carry no free text a caller
could not already see, so the same field-masking policy the rest of the API applies is enough at the
boundary.

Everything monetary is integer cents (design §1.1). ``evidence`` is a small, JSON-serializable
structure so it round-trips through the ``evidence`` JSON column without a bespoke encoder and so a
UI can render "why" chips without a second call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum, StrEnum


class SignalType(StrEnum):
    """The closed set of signals the detectors emit (task 17.2).

    Each maps to a deterministic detector over an existing service:

    * ``RISK_BAND_UP`` — the customer's risk band moved up a bucket (risk profile + derived band).
    * ``AML_PEP_FLAG`` — an AML or PEP compliance flag is active (risk profile).
    * ``LARGE_DEPOSIT`` — a single credit above the configured floor opened a cross-sell window
      (expense analytics / transactions).
    * ``LIFE_EVENT`` — a life event was detected, corroborated by transactions (journey).
    """

    RISK_BAND_UP = "RISK_BAND_UP"
    AML_PEP_FLAG = "AML_PEP_FLAG"
    LARGE_DEPOSIT = "LARGE_DEPOSIT"
    LIFE_EVENT = "LIFE_EVENT"


class Severity(IntEnum):
    """Signal severity, ordered so a plain ``sort`` puts the most urgent last.

    An ``IntEnum`` (like :class:`c360.services.risk.AlertSeverity`) so the ranker can weight it
    numerically and a descending sort is ``-severity``. The string names are what the schema's
    ``CHECK`` constrains and what the UI renders — never a colour alone (task 17.6).
    """

    INFO = 1
    WARNING = 2
    CRITICAL = 3


class SignalStatus(StrEnum):
    """Per-user lifecycle of a signal (task 17.1).

    ``NEW`` is the implicit state of a freshly-detected signal with no state row; ``SEEN`` /
    ``DISMISSED`` / ``ACTIONED`` are written to ``signal_state`` when a user interacts with it.
    Dismissed signals are suppressed from the queue for the cooling-off window (task 17.3).
    """

    NEW = "NEW"
    SEEN = "SEEN"
    DISMISSED = "DISMISSED"
    ACTIONED = "ACTIONED"


@dataclass(frozen=True, slots=True)
class Citation:
    """One fact citation backing a signal (task 17.2, requirement 10.8).

    Mirrors the tool layer's fact-wrapping fields (``entity_type``/``entity_id``/``field``) so a
    signal's evidence resolves against the same customer data the 360 view shows, and so the feed is
    grounded and inspectable rather than an opaque score.
    """

    entity_type: str
    entity_id: str
    field: str
    as_of: str

    def as_dict(self) -> dict[str, str]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field": self.field,
            "as_of": self.as_of,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Citation:
        return cls(
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            field=data["field"],
            as_of=data["as_of"],
        )


@dataclass(frozen=True, slots=True)
class Evidence:
    """The typed "why" behind a signal (task 17.2).

    ``summary`` is a short, value-free label safe to render and to log; ``citations`` point at the
    facts that produced the signal; ``details`` is a small map of already-legible qualifiers (a
    band name, a life-event type, a bucketed magnitude) the UI renders as chips. Monetary magnitudes
    live in the signal's ``value_at_stake_cents``, not here, so masking governs them.
    """

    summary: str
    citations: tuple[Citation, ...] = ()
    details: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "citations": [citation.as_dict() for citation in self.citations],
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Evidence:
        raw_citations = data.get("citations", [])
        citations_iter = raw_citations if isinstance(raw_citations, list) else []
        citations = tuple(
            Citation.from_dict(item) for item in citations_iter if isinstance(item, dict)
        )
        raw_details = data.get("details", {})
        details = (
            {str(k): str(v) for k, v in raw_details.items()}
            if isinstance(raw_details, dict)
            else {}
        )
        return cls(summary=str(data.get("summary", "")), citations=citations, details=details)


@dataclass(frozen=True, slots=True)
class DetectedSignal:
    """A signal as a detector emits it, before it is persisted (task 17.2).

    ``dedup_key`` is the stable identity of a *live* concern: re-running detection with the same
    underlying condition produces the same key so the store updates in place rather than inserting a
    duplicate (task 17.1, 17.4). It is deterministic — derived from customer, type and the specific
    condition — never from a timestamp.
    """

    customer_id: str
    signal_type: SignalType
    severity: Severity
    value_at_stake_cents: int
    evidence: Evidence
    as_of: date
    dedup_key: str


@dataclass(frozen=True, slots=True)
class Signal:
    """A persisted signal read back from ``signals.db`` (task 17.1).

    ``score`` is the ranker's output, stored so the queue orders without recomputing and so a run is
    reproducible. ``status`` is the caller's per-user state folded in at read time (``NEW`` when no
    state row exists). ``segment`` is carried for entitlement scoping and for the ``segment`` metric
    label, not shown.
    """

    signal_id: int
    customer_id: str
    signal_type: SignalType
    severity: Severity
    score: float
    value_at_stake_cents: int
    evidence: Evidence
    as_of: date
    detected_at: str
    dedup_key: str
    status: SignalStatus = SignalStatus.NEW


__all__ = [
    "Citation",
    "DetectedSignal",
    "Evidence",
    "Severity",
    "Signal",
    "SignalStatus",
    "SignalType",
]
