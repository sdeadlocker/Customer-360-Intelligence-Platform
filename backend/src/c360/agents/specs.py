"""The declarative inventory of the seven dashboard agents (task 8.7, design §8.3).

One :class:`AgentSpec` per agent captures everything the graph and the runner need without any agent
being a bespoke class: its wave and timeout (which edge it sits on and its ``asyncio.wait_for``
budget), the customer tools whose facts it consumes, the knowledge domains it retrieves (empty for
the four that do not), its output schema, and two pure functions — ``build_outputs`` that fills the
structured schema deterministically from the fact table, and ``confidence`` that derives a 0..1
figure from fact coverage. The field allowlist is *not* stored here: it is read from the versioned
prompt file at build time (task 8.3), so the reviewed prompt remains the single source of truth for
what a model may see.

Design §8.3 fixes the inventory. Only Risk, Life Event and Offer retrieve; the other four stay
purely customer-grounded because retrieval would add latency without adding anything their
narratives need.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from c360.agents import schemas

if TYPE_CHECKING:
    from pydantic import BaseModel

    from c360.tools.facts import FactTable


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One agent's static definition (design §8.2/8.3)."""

    name: str
    wave: int
    timeout_s: float
    #: Customer tools whose fact tables feed this agent; ``load_context`` runs them once and unions
    #: their facts into this agent's table.
    source_tools: tuple[str, ...]
    #: Knowledge domains to retrieve for this agent. Empty means the agent does not retrieve.
    knowledge_domains: tuple[str, ...]
    task: str
    output_model: type[BaseModel]
    build_outputs: Callable[[str, FactTable], BaseModel]
    confidence: Callable[[FactTable, tuple[str, ...]], float | None]
    max_tokens: int = 1024
    #: Filled from the prompt registry at build time; not part of the static definition.
    field_allowlist: frozenset[str] = field(default=frozenset())

    def with_allowlist(self, allowlist: frozenset[str]) -> AgentSpec:
        """Return a copy bound to the field allowlist read from this agent's prompt file."""
        return self._replace(field_allowlist=allowlist)

    def with_timeout(self, timeout_s: float) -> AgentSpec:
        """Return a copy with the per-node ``asyncio.wait_for`` budget overridden.

        The design fixes tight p95 budgets (2.5 / 1.5 / 1.0 s), but those assume in-region, warmed
        Bedrock. A live demo over the public internet to a cold model routinely exceeds them, which
        trips every node's timeout and blanks every card. The graph therefore applies the
        configurable ``AGENT_WAVE*_BUDGET_S`` here so an operator can relax the budget without
        editing the design's numbers.
        """
        return self._replace(timeout_s=timeout_s)

    def _replace(
        self,
        *,
        timeout_s: float | None = None,
        field_allowlist: frozenset[str] | None = None,
    ) -> AgentSpec:
        return AgentSpec(
            name=self.name,
            wave=self.wave,
            timeout_s=self.timeout_s if timeout_s is None else timeout_s,
            source_tools=self.source_tools,
            knowledge_domains=self.knowledge_domains,
            task=self.task,
            output_model=self.output_model,
            build_outputs=self.build_outputs,
            confidence=self.confidence,
            max_tokens=self.max_tokens,
            field_allowlist=self.field_allowlist if field_allowlist is None else field_allowlist,
        )


# ---------------------------------------------------------------- confidence


def _coverage_confidence(facts: FactTable, unavailable: tuple[str, ...]) -> float | None:
    """A blunt but honest confidence: fewer facts and more missing inputs lower it.

    Not a calibrated probability — design §8.2 types ``confidence`` as optional and advisory. It
    exists so the UI can de-emphasise a card built on thin data, and so a completely fact-less agent
    reports ``None`` (no basis) rather than a misleading number.
    """
    if not facts.facts:
        return None
    penalty = 0.15 * len(unavailable)
    return max(0.1, min(1.0, 1.0 - penalty))


# ---------------------------------------------------------------- output builders


def _facts_by_field(facts: FactTable) -> dict[str, object]:
    """Latest value per field name, for pulling a specific figure into a structured slot."""
    return {fact.field: fact.value for fact in facts.facts}


def _fact_id_for(facts: FactTable, field_name: str) -> str | None:
    for fact in facts.facts:
        if fact.field == field_name:
            return fact.fact_id
    return None


def _snapshot(facts: FactTable, fields: tuple[str, ...]) -> tuple[schemas.SnapshotItem, ...]:
    items: list[schemas.SnapshotItem] = []
    for name in fields:
        fact_id = _fact_id_for(facts, name)
        if fact_id is not None:
            items.append(schemas.SnapshotItem(label=name, fact_id=fact_id))
    return tuple(items)


def _financial_outputs(narrative: str, facts: FactTable) -> schemas.FinancialHealthOutput:
    return schemas.FinancialHealthOutput(
        narrative=narrative,
        snapshot=_snapshot(
            facts, ("net_worth_cents", "total_deposits_cents", "total_loans_cents", "fico_score")
        ),
    )


def _risk_outputs(narrative: str, facts: FactTable) -> schemas.RiskOutput:
    by_field = _facts_by_field(facts)
    band = by_field.get("band")
    return schemas.RiskOutput(
        narrative=narrative,
        risk_band=str(band) if band is not None else None,
    )


def _life_event_outputs(narrative: str, facts: FactTable) -> schemas.LifeEventOutput:
    return schemas.LifeEventOutput(narrative=narrative)


def _relationship_outputs(narrative: str, facts: FactTable) -> schemas.RelationshipOutput:
    return schemas.RelationshipOutput(narrative=narrative)


def _offer_outputs(narrative: str, facts: FactTable) -> schemas.OfferOutput:
    return schemas.OfferOutput(narrative=narrative)


def _journey_outputs(narrative: str, facts: FactTable) -> schemas.JourneyOutput:
    return schemas.JourneyOutput(narrative=narrative)


def _summary_outputs(narrative: str, facts: FactTable) -> schemas.SummaryOutput:
    return schemas.SummaryOutput(
        narrative=narrative,
        executive_summary=narrative,
        snapshot=_snapshot(facts, ("customer_segment", "net_worth_cents", "band")),
    )


# ---------------------------------------------------------------- the inventory

_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="financial_health",
        wave=1,
        timeout_s=2.5,
        source_tools=("profile", "financial_profile", "credit_profile", "expense_analytics"),
        knowledge_domains=(),
        task="Summarise the customer's financial health from the facts.",
        output_model=schemas.FinancialHealthOutput,
        build_outputs=_financial_outputs,
        confidence=_coverage_confidence,
    ),
    AgentSpec(
        name="risk",
        wave=1,
        timeout_s=2.5,
        source_tools=("risk_profile", "credit_profile"),
        knowledge_domains=("procedure",),
        task="Assess the customer's risk from the facts and cite prescribed procedure steps.",
        output_model=schemas.RiskOutput,
        build_outputs=_risk_outputs,
        confidence=_coverage_confidence,
    ),
    AgentSpec(
        name="life_event",
        wave=1,
        timeout_s=2.5,
        source_tools=("profile",),
        knowledge_domains=("playbook",),
        task="Summarise detected life events and cite relevant playbook guidance.",
        output_model=schemas.LifeEventOutput,
        build_outputs=_life_event_outputs,
        confidence=_coverage_confidence,
    ),
    AgentSpec(
        name="relationship",
        wave=1,
        timeout_s=2.5,
        source_tools=("household",),
        knowledge_domains=(),
        task="Summarise the customer's household and relationship network from the facts.",
        output_model=schemas.RelationshipOutput,
        build_outputs=_relationship_outputs,
        confidence=_coverage_confidence,
    ),
    AgentSpec(
        name="offer_recommendation",
        wave=2,
        timeout_s=1.5,
        source_tools=("offers", "credit_profile", "profile"),
        knowledge_domains=("product_catalog", "offer_terms"),
        task="Recommend next-best offers, cite eligibility from product knowledge.",
        output_model=schemas.OfferOutput,
        build_outputs=_offer_outputs,
        confidence=_coverage_confidence,
    ),
    AgentSpec(
        name="journey",
        wave=2,
        timeout_s=1.5,
        source_tools=("profile",),
        knowledge_domains=(),
        task="Narrate the customer's journey and growth story from the facts.",
        output_model=schemas.JourneyOutput,
        build_outputs=_journey_outputs,
        confidence=_coverage_confidence,
    ),
    AgentSpec(
        name="customer_summary",
        wave=3,
        timeout_s=1.0,
        source_tools=("profile", "financial_profile", "risk_profile"),
        knowledge_domains=(),
        task="Produce an executive summary from the facts and upstream agent outputs.",
        output_model=schemas.SummaryOutput,
        build_outputs=_summary_outputs,
        confidence=_coverage_confidence,
    ),
)

#: Every agent spec, keyed by name.
AGENT_SPECS: dict[str, AgentSpec] = {spec.name: spec for spec in _SPECS}

#: Agent names by wave, in the fan-out order design §8.1 fixes.
WAVE_1: tuple[str, ...] = ("financial_health", "risk", "life_event", "relationship")
WAVE_2: tuple[str, ...] = ("offer_recommendation", "journey")
WAVE_3: tuple[str, ...] = ("customer_summary",)


__all__ = ["AGENT_SPECS", "WAVE_1", "WAVE_2", "WAVE_3", "AgentSpec"]
