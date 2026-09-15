"""Report value types (task 18.1, 18.2, 18.4, 18.5).

A *report definition* describes what to produce: its type (a branded 360 PDF pack or a
prepare-for-meeting briefing), the scope it runs over (one customer, a book, or a segment), the
branding to stamp on it, and who owns it. A *schedule* attaches a cron-like cadence and an owner
whose entitlement is re-checked at generation time. A *run* is one execution: its status, the
artifact it produced on disk, and the provenance (model id, prompt version, degraded flag) carried
from the AI narratives so a reader can see how each figure and talking point was produced.

These types are the shape written into ``reports.db`` and read back for the API. They carry no
monetary values and no free-text customer data — the artifacts on disk carry the (already masked)
content; the database rows carry only structure, status and provenance, so nothing sensitive lives
in a queryable table (design §1.1, requirement 12.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ReportType(StrEnum):
    """The closed set of report kinds Phase 18 produces.

    * ``PDF_PACK`` — a branded 360 PDF pack per customer, assembled from the masked service reads
      (task 18.2).
    * ``MEETING_BRIEFING`` — a prepare-for-meeting briefing composing the 360 summary, open signals
      and cited talking points (task 18.3).
    """

    PDF_PACK = "PDF_PACK"
    MEETING_BRIEFING = "MEETING_BRIEFING"


class ReportScopeKind(StrEnum):
    """What a report runs over (task 18.1).

    * ``CUSTOMER`` — a single customer, named by ``customer_id``.
    * ``BOOK`` — an explicit set of customer ids the owner is entitled to.
    * ``SEGMENT`` — every customer in one or more segments, within the owner's entitlement.
    """

    CUSTOMER = "CUSTOMER"
    BOOK = "BOOK"
    SEGMENT = "SEGMENT"


class RunStatus(StrEnum):
    """The lifecycle of one report run (task 18.1)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Branding:
    """The cover branding stamped on a generated report (task 18.1, 18.2).

    Value-free: a bank name and a tagline, never customer data. A definition that sets none inherits
    the platform defaults from :class:`~c360.core.config.Settings`.
    """

    brand_name: str
    tagline: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"brand_name": self.brand_name, "tagline": self.tagline}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Branding:
        return cls(
            brand_name=str(data.get("brand_name", "")),
            tagline=str(data.get("tagline", "")),
        )


@dataclass(frozen=True, slots=True)
class ReportScope:
    """The target a report runs over (task 18.1).

    Exactly one of ``customer_id`` / ``customer_ids`` / ``segments`` is meaningful per ``kind``; the
    others are empty. Customers and segments are bare references — there is no foreign key into
    ``customer.db`` (task 18.1) — and entitlement is enforced at generation time against the owner's
    live scope, never trusted from what is stored here.
    """

    kind: ReportScopeKind
    customer_id: str | None = None
    customer_ids: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": str(self.kind),
            "customer_id": self.customer_id,
            "customer_ids": list(self.customer_ids),
            "segments": list(self.segments),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ReportScope:
        raw_ids = data.get("customer_ids", [])
        ids = tuple(str(cid) for cid in raw_ids) if isinstance(raw_ids, list) else ()
        raw_segments = data.get("segments", [])
        segments = tuple(str(s) for s in raw_segments) if isinstance(raw_segments, list) else ()
        raw_customer = data.get("customer_id")
        return cls(
            kind=ReportScopeKind(str(data["kind"])),
            customer_id=str(raw_customer) if raw_customer is not None else None,
            customer_ids=ids,
            segments=segments,
        )


@dataclass(frozen=True, slots=True)
class ReportDefinition:
    """A persisted report definition (task 18.1)."""

    definition_id: int
    report_type: ReportType
    title: str
    scope: ReportScope
    branding: Branding
    owner_id: str
    owner_role: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReportSchedule:
    """A cron-like cadence attached to a definition (task 18.1, 18.4).

    ``cadence`` is a small, closed vocabulary (``DAILY`` / ``WEEKLY`` / ``MONTHLY``) rather than a
    full cron expression: the platform's scheduled path is "run every due schedule" invoked by
    ``c360 run-reports`` (task 18.5), so a coarse cadence is enough and keeps the due-check simple
    and testable. ``owner_id`` / ``owner_role`` are the entitlement snapshot the schedule was
    created under; the scheduler re-checks the owner's *live* entitlement at generation time and
    never trusts this snapshot (task 18.4).
    """

    schedule_id: int
    definition_id: int
    cadence: str
    owner_id: str
    owner_role: str
    active: bool
    last_run_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class RunProvenance:
    """How a run's AI narratives were produced, carried from the agent results (task 18.2, 18.3).

    Value-free: model ids, prompt versions and a degraded flag, never a figure or a customer
    detail. Stored so a reader of the run history can see whether a pack used real Bedrock
    narratives or the deterministic template, without opening the artifact.
    """

    model_ids: tuple[str, ...] = ()
    prompt_versions: tuple[str, ...] = ()
    degraded: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "model_ids": list(self.model_ids),
            "prompt_versions": list(self.prompt_versions),
            "degraded": self.degraded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RunProvenance:
        raw_models = data.get("model_ids", [])
        raw_prompts = data.get("prompt_versions", [])
        return cls(
            model_ids=tuple(str(m) for m in raw_models) if isinstance(raw_models, list) else (),
            prompt_versions=(
                tuple(str(p) for p in raw_prompts) if isinstance(raw_prompts, list) else ()
            ),
            degraded=bool(data.get("degraded", False)),
        )


@dataclass(frozen=True, slots=True)
class ReportRun:
    """A persisted report run (task 18.1)."""

    run_id: int
    definition_id: int
    report_type: ReportType
    status: RunStatus
    triggered_by: str
    trigger_kind: str
    artifact_path: str | None
    customers_rendered: int
    provenance: RunProvenance
    started_at: str
    finished_at: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedArtifact:
    """The in-memory result of generating a report, before it is written to disk (task 18.2)."""

    content: bytes
    media_type: str
    file_extension: str
    customers_rendered: int
    provenance: RunProvenance = field(default_factory=RunProvenance)


__all__ = [
    "Branding",
    "GeneratedArtifact",
    "ReportDefinition",
    "ReportRun",
    "ReportSchedule",
    "ReportScope",
    "ReportScopeKind",
    "ReportType",
    "RunProvenance",
    "RunStatus",
]
