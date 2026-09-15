"""Per-agent Pydantic output schemas (task 8.7, design §8.3).

Each agent returns a *typed* payload, not a paragraph, so the UI renders inspectable structure and
evaluator can score fields rather than parse prose (design §8.2). Every schema carries a
``narrative`` — the model's (or the template's) grounded prose — plus a small set of structured
fields the design names for that agent (drivers, snapshot items, ranked offers, ...).

A deliberate constraint: the structured fields are populated *deterministically from the fact table*
by the agent runner, not parsed out of the model's text. The model writes the narrative; the runner
fills the structure from the same facts the narrative was grounded in. This keeps the structured
view exactly as trustworthy as the facts, and it is what lets the mock and a live model produce the
same schema without the mock having to imitate a model's JSON.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Output(BaseModel):
    """Base for every agent output: frozen, with the narrative every card shows."""

    model_config = ConfigDict(frozen=True)

    narrative: str = ""


class SnapshotItem(BaseModel):
    """One inspectable key fact: a label, a fact-cited value, and the fact id it came from."""

    model_config = ConfigDict(frozen=True)

    label: str
    fact_id: str = Field(pattern=r"^F\d+$")


class HealthDriver(BaseModel):
    """One driver of the financial-health score (design §7.3)."""

    model_config = ConfigDict(frozen=True)

    factor: str
    contribution: int
    detail: str


class FinancialHealthOutput(_Output):
    """Financial Health agent (§8.3): net worth analysis, spending insight, health drivers."""

    health_score: int | None = None
    health_band: str | None = None
    drivers: tuple[HealthDriver, ...] = ()
    snapshot: tuple[SnapshotItem, ...] = ()


class RiskDriver(BaseModel):
    """One ranked risk driver."""

    model_config = ConfigDict(frozen=True)

    factor: str
    severity: str


class RiskOutput(_Output):
    """Risk agent (design §8.3): assessment, ranked drivers, alerts, prescribed next steps."""

    risk_band: str | None = None
    drivers: tuple[RiskDriver, ...] = ()
    alerts: tuple[str, ...] = ()
    #: Non-dismissible compliance signal — surfaced separately so the UI can render it as a banner.
    compliance_flag: bool = False
    next_steps: tuple[str, ...] = ()


class LifeEventItem(BaseModel):
    """One life event with its confidence and corroboration."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    confidence: float | None = None
    inferred: bool = False


class LifeEventOutput(_Output):
    """Life Event agent (design §8.3): timeline with confidence, recommendations."""

    events: tuple[LifeEventItem, ...] = ()
    recommendations: tuple[str, ...] = ()


class RelationshipOutput(_Output):
    """Relationship Intelligence agent (design §8.3): household view, network insights."""

    household_id: str | None = None
    member_count: int | None = None
    insights: tuple[str, ...] = ()


class RankedOffer(BaseModel):
    """One ranked offer with its rationale and suppression status."""

    model_config = ConfigDict(frozen=True)

    offer_id: str
    rationale: str
    suppressed: bool = False
    suppression_reason: str | None = None


class OfferOutput(_Output):
    """Offer Recommendation agent (design §8.3): NBO, ranked list, eligibility, suppression."""

    ranked_offers: tuple[RankedOffer, ...] = ()
    eligibility_notes: tuple[str, ...] = ()


class JourneyMilestone(BaseModel):
    """One milestone on the journey timeline."""

    model_config = ConfigDict(frozen=True)

    milestone_type: str
    detail: str


class JourneyOutput(_Output):
    """Customer Journey agent (design §8.3): timeline narrative, growth story."""

    milestones: tuple[JourneyMilestone, ...] = ()


class AdvisorNote(BaseModel):
    """One actionable advisor note in the executive summary."""

    model_config = ConfigDict(frozen=True)

    note: str


class SummaryOutput(_Output):
    """Customer Summary agent (design §8.2/8.3): executive summary, snapshot, advisor notes."""

    executive_summary: str = ""
    snapshot: tuple[SnapshotItem, ...] = ()
    advisor_notes: tuple[AdvisorNote, ...] = ()


__all__ = [
    "AdvisorNote",
    "FinancialHealthOutput",
    "HealthDriver",
    "JourneyMilestone",
    "JourneyOutput",
    "LifeEventItem",
    "LifeEventOutput",
    "OfferOutput",
    "RankedOffer",
    "RelationshipOutput",
    "RiskDriver",
    "RiskOutput",
    "SnapshotItem",
    "SummaryOutput",
]
