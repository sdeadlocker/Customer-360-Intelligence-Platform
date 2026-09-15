"""Compose report artifacts from masked, entitlement-scoped service reads (task 18.2, 18.3).

The one rule that makes a report safe: it is assembled from **the same masked service reads the REST
API uses**, so a report can never contain a field the requesting role may not see (task 18.2,
requirement 12.4). Concretely, every domain model that goes into a report is first run through
:func:`c360.security.serializer.mask_model` under the owner's :class:`~c360.security.policy.
FieldPolicy`; the renderer then reads the *masked dict*, never the raw model. A hidden field is
simply absent, a banded balance is a band label, and the PDF shows exactly what the API would return
to that role.

Two artifacts:

* :func:`render_pdf_pack` — a branded 360 PDF pack per customer (task 18.2): cover, profile,
  financial headline, risk band and alerts, top offers, and any AI narratives with their generation
  labels and citations.
* :func:`render_meeting_briefing` — a prepare-for-meeting briefing (task 18.3): the 360 summary,
  the customer's open signals (Phase 17) and cited talking points, one section per customer.

Both are deterministic given their inputs (the PDF writer emits no timestamps), so a re-run over
unchanged data is byte-identical — the property the report tests assert.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from c360.reports.models import Branding, GeneratedArtifact, RunProvenance
from c360.reports.pdf import PdfBuilder
from c360.security.serializer import mask_model

if TYPE_CHECKING:
    from pydantic import BaseModel

    from c360.agents.result import AgentResult
    from c360.security.policy import FieldPolicy
    from c360.services.aggregator import Customer360
    from c360.signals.models import Signal


def _masked(model: BaseModel | None, policy: FieldPolicy) -> dict[str, Any]:
    """Serialize a model under the owner's field policy, or ``{}`` when the module was absent."""
    if model is None:
        return {}
    data, _fields = mask_model(model, policy)
    return data


def _value(data: dict[str, Any], *keys: str, default: str = "—") -> str:
    """Read the first present key from a masked dict, as a display string.

    A masked-away key is *absent*, so this returns the ``default`` — which is exactly the
    "restricted / not shown" affordance a report should render for a field the role cannot see.
    """
    for key in keys:
        if key in data and data[key] is not None:
            return str(data[key])
    return default


# ==================================================================== PDF pack (task 18.2)
def _cover(builder: PdfBuilder, branding: Branding, title: str) -> None:
    builder.heading(branding.brand_name)
    if branding.tagline:
        builder.body(branding.tagline, italic=True)
    builder.rule()
    builder.subheading(title)


def _render_customer_section(
    builder: PdfBuilder,
    view: Customer360,
    policy: FieldPolicy,
    narratives: dict[str, AgentResult],
) -> RunProvenance:
    """Render one customer's section into the PDF, from masked reads. Returns its AI provenance."""
    profile = _masked(view.profile, policy)
    builder.subheading(_value(profile, "customer_name", default=view.customer_id))
    builder.body(
        f"Segment: {_value(profile, 'customer_segment')}  ·  "
        f"Value tier: {_value(profile, 'customer_value')}  ·  "
        f"Customer since: {_value(profile, 'customer_since')}"
    )

    contact = _masked(view.contact, policy)
    builder.body(
        f"Email: {_value(contact, 'email')}  ·  "
        f"Mobile: {_value(contact, 'mobile', 'phone')}  ·  "
        f"City: {_value(contact, 'city')}"
    )

    _render_financial(builder, view, policy)
    _render_risk(builder, view, policy)
    _render_offers(builder, view, policy)
    return _render_narratives(builder, narratives)


def _render_financial(builder: PdfBuilder, view: Customer360, policy: FieldPolicy) -> None:
    fin = _masked(view.financial_profile, policy)
    if not fin:
        return
    builder.spacer(4.0)
    builder.body("Financial profile", italic=False)
    builder.body(
        f"Net worth: {_value(fin, 'net_worth_cents', 'net_worth')}  ·  "
        f"Total relationship balance: "
        f"{_value(fin, 'total_relationship_balance_cents', 'total_relationship_balance')}"
    )


def _render_risk(builder: PdfBuilder, view: Customer360, policy: FieldPolicy) -> None:
    if view.risk is None:
        return
    risk = view.risk
    builder.spacer(4.0)
    builder.body(f"Risk band: {risk.band.value}")
    if risk.requires_compliance_indicator:
        builder.body("Compliance indicator active (non-dismissible).")
    for alert in risk.alerts[:5]:
        builder.body(f"  • [{alert.category}] {alert.detail} ({alert.severity.name})")


def _render_offers(builder: PdfBuilder, view: Customer360, policy: FieldPolicy) -> None:
    if view.offers is None or not view.offers.offers:
        return
    builder.spacer(4.0)
    builder.body("Top offers")
    live = [offer for offer in view.offers.offers if not offer.suppressed][:3]
    for ranked in live:
        kind = "cross-sell" if ranked.is_cross_sell else "upsell" if ranked.is_upsell else "offer"
        builder.body(
            f"  • #{ranked.rank} {ranked.offer.offer.offer_name} ({kind}) — {ranked.rationale}"
        )


def _render_narratives(builder: PdfBuilder, narratives: dict[str, AgentResult]) -> RunProvenance:
    """Render AI narratives with their generation labels and citations (task 18.2)."""
    if not narratives:
        return RunProvenance()
    builder.spacer(4.0)
    builder.body("AI insights")
    model_ids: set[str] = set()
    prompt_versions: set[str] = set()
    degraded = False
    for agent in sorted(narratives):
        result = narratives[agent]
        model_ids.add(result.model_id)
        prompt_versions.add(result.prompt_version)
        degraded = degraded or result.degraded
        text = _narrative_text(result)
        builder.body(f"  {agent}: {text}")
        label = (
            f"    generated {result.generated_at.date().isoformat()} · model {result.model_id} · "
            f"prompt {result.prompt_version}" + (" · degraded fallback" if result.degraded else "")
        )
        builder.caption(label)
        cite = _citation_line(result)
        if cite:
            builder.caption(f"    {cite}")
    return RunProvenance(
        model_ids=tuple(sorted(model_ids)),
        prompt_versions=tuple(sorted(prompt_versions)),
        degraded=degraded,
    )


def _narrative_text(result: AgentResult) -> str:
    """A short, human-readable summary line for an agent's output.

    Prefers a ``summary``/``narrative``/``headline`` field on the output schema; falls back to the
    agent name so a card with an unusual schema still renders a line rather than nothing.
    """
    dumped = result.outputs.model_dump(mode="json")
    for key in ("summary", "narrative", "headline", "assessment", "recommendation"):
        value = dumped.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return result.agent.replace("_", " ")


def _citation_line(result: AgentResult) -> str:
    """A compact citation line: fact ids and passage doc/section refs (task 18.2)."""
    facts = [c.fact_id for c in result.fact_citations]
    passages = [f"{c.doc_id}#{c.section_path}" for c in result.passage_citations]
    parts: list[str] = []
    if facts:
        parts.append("facts: " + ", ".join(facts))
    if passages:
        parts.append("sources: " + "; ".join(passages))
    return "  ".join(parts)


def render_pdf_pack(
    *,
    branding: Branding,
    title: str,
    sections: list[tuple[Customer360, dict[str, AgentResult]]],
    policy: FieldPolicy,
) -> GeneratedArtifact:
    """Assemble a branded 360 PDF pack over one or more customers (task 18.2)."""
    builder = PdfBuilder()
    _cover(builder, branding, title)
    model_ids: set[str] = set()
    prompt_versions: set[str] = set()
    degraded = False
    for index, (view, narratives) in enumerate(sections):
        if index > 0:
            builder.page_break()
        else:
            builder.spacer(6.0)
        prov = _render_customer_section(builder, view, policy, narratives)
        model_ids.update(prov.model_ids)
        prompt_versions.update(prov.prompt_versions)
        degraded = degraded or prov.degraded
    return GeneratedArtifact(
        content=builder.build(),
        media_type="application/pdf",
        file_extension="pdf",
        customers_rendered=len(sections),
        provenance=RunProvenance(
            model_ids=tuple(sorted(model_ids)),
            prompt_versions=tuple(sorted(prompt_versions)),
            degraded=degraded,
        ),
    )


# ==================================================================== briefing (task 18.3)
def _render_signals(builder: PdfBuilder, signals: tuple[Signal, ...]) -> None:
    """Render a customer's open signals as talking points (task 18.3)."""
    open_signals = [s for s in signals if s.status.value in ("NEW", "SEEN")]
    if not open_signals:
        builder.body("No open signals.")
        return
    builder.body("Open signals")
    for signal in open_signals[:8]:
        builder.body(
            f"  • [{signal.signal_type.value}] {signal.evidence.summary} "
            f"(severity {signal.severity.name})"
        )
        cites = [f"{c.entity_type}:{c.field}" for c in signal.evidence.citations]
        if cites:
            builder.caption("    grounded in: " + ", ".join(cites))


def render_meeting_briefing(
    *,
    branding: Branding,
    title: str,
    sections: list[tuple[Customer360, dict[str, AgentResult], tuple[Signal, ...]]],
    policy: FieldPolicy,
) -> GeneratedArtifact:
    """Assemble a prepare-for-meeting briefing (task 18.3).

    Each section composes the 360 summary (the customer_summary agent, or the profile when the AI
    layer is off), the customer's open signals, and cited talking points drawn from the agent
    narratives — all from masked reads under the owner's policy.
    """
    builder = PdfBuilder()
    _cover(builder, branding, title)
    model_ids: set[str] = set()
    prompt_versions: set[str] = set()
    degraded = False
    for index, (view, narratives, signals) in enumerate(sections):
        if index > 0:
            builder.page_break()
        else:
            builder.spacer(6.0)
        profile = _masked(view.profile, policy)
        builder.subheading(
            "Meeting brief: " + _value(profile, "customer_name", default=view.customer_id)
        )
        summary = narratives.get("customer_summary")
        if summary is not None:
            builder.body(_narrative_text(summary))
            builder.caption(
                f"generated {summary.generated_at.date().isoformat()} · model {summary.model_id} · "
                f"prompt {summary.prompt_version}"
                + (" · degraded fallback" if summary.degraded else "")
            )
        else:
            builder.body(
                f"Segment {_value(profile, 'customer_segment')}, value tier "
                f"{_value(profile, 'customer_value')}."
            )
        builder.spacer(4.0)
        _render_signals(builder, signals)
        builder.spacer(4.0)
        builder.body("Talking points")
        prov = _render_narratives(builder, narratives)
        model_ids.update(prov.model_ids)
        prompt_versions.update(prov.prompt_versions)
        degraded = degraded or prov.degraded
    return GeneratedArtifact(
        content=builder.build(),
        media_type="application/pdf",
        file_extension="pdf",
        customers_rendered=len(sections),
        provenance=RunProvenance(
            model_ids=tuple(sorted(model_ids)),
            prompt_versions=tuple(sorted(prompt_versions)),
            degraded=degraded,
        ),
    )


__all__ = ["render_meeting_briefing", "render_pdf_pack"]
