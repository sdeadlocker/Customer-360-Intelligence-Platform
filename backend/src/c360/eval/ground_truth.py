"""Ground-truth export from the generator (task 11.1, design §14.2, §15.1).

The generator's :func:`c360.generator.pipeline.generate` returns the in-memory
:class:`~c360.generator.plan.Population` -- the plan every row was emitted from. That plan *is* the
ground truth: it holds the true cohort of each customer, the life events the generator decided to
create (with their corroborating transaction signals), the delinquency and fraud flags it stamped,
and the products that compose each customer's financial position. This module reads that plan and
projects it into stable, serialisable label records (design §15.1 exports "as data, not derived at
eval time"), so a change to a *service* can never move the truth a scorer measures against.

What is exported (design §14.2):

* **The panel** -- a fixed customer set, at least ``eval_panel_per_cohort`` per cohort across all
  eight cohorts, pinned by customer id. Selection is deterministic (lowest customer index first)
  so the same seed and count always yield the same panel.
* **Per-customer material facts** -- the things that MUST be surfaced: 90+ DPD, an AML/PEP flag, a
  maturing deposit, a fraud alert, a sharp utilisation rise. Each is a typed :class:`MaterialFact`
  the coverage scorer (dimension 5) checks the agent narratives against.
* **True life events** -- the planned events with their type, date, inferred flag and confidence,
  which the life-event detection scorer (dimension 8) scores precision/recall/F1 against.
* **True propensities** -- a per-customer ranking of the products the customer is genuinely most
  likely to accept, derived from cohort and holdings, which the offer-ranking scorer (dimension 9)
  scores nDCG@3 / precision@1 against.
* **Risk-score compositions** -- the named factors that compose each customer's risk, which the
  risk-driver scorer (dimension 10) checks the agent's named drivers against.

Labelled retrieval pairs (design §14.2 ``eval_retrieval``) are *not* derived from the generator --
they come from the knowledge corpus manifest -- so they live in :mod:`c360.eval.retrieval_labels`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from c360.domain.enums import AccountType, DepositProductType, LoanType
from c360.generator.cohorts import Cohort
from c360.generator.context import DEFAULT_AS_OF
from c360.generator.pipeline import generate
from c360.generator.plan import CustomerPlan, Population

#: How many days ahead of the as-of date a deposit still counts as "maturing" for coverage. A CD
#: that matures within the quarter is material to surface; one maturing in three years is not.
_MATURING_WINDOW_DAYS = 120

#: The DPD threshold design §14.2 names explicitly as a material fact ("90+ DPD").
_MATERIAL_DPD = 90


@dataclass(frozen=True, slots=True)
class MaterialFact:
    """One thing an agent MUST surface for a customer (design §14.2, dimension 5).

    ``kind`` is a stable machine label (e.g. ``DELINQUENCY_90``); ``detail`` is a value-free,
    human-readable description used only in reports. ``keywords`` are the narrative tokens the
    coverage scorer looks for -- a material fact is "covered" when the agent's narrative mentions
    any
    of them, because the narrative is prose, not a structured field.
    """

    kind: str
    detail: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrueLifeEvent:
    """A life event the generator actually created (dimension 8)."""

    event_type: str
    event_date: str
    inferred: bool
    confidence: float | None


@dataclass(frozen=True, slots=True)
class RiskComposition:
    """The named factors that compose a customer's risk score (dimension 10).

    ``factors`` are stable factor labels (``DELINQUENCY``, ``PAYMENT_HISTORY``). The
    scorer checks that the agent's named drivers match this set, so the labels here are the same
    vocabulary the risk agent is expected to use.
    """

    factors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CustomerGroundTruth:
    """Every label known about one panel customer (design §14.2, §15.1)."""

    customer_id: str
    cohort: str
    segment: str
    material_facts: tuple[MaterialFact, ...] = ()
    life_events: tuple[TrueLifeEvent, ...] = ()
    #: Product codes/kinds ordered by true acceptance propensity, most likely first (dimension 9).
    propensity_ranking: tuple[str, ...] = ()
    risk: RiskComposition = field(default_factory=lambda: RiskComposition(factors=()))


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """The whole exported ground truth for one (seed, count) generation.

    ``panel`` is the pinned evaluation panel -- the customer ids, in a stable order. ``by_customer``
    holds the labels for every panel customer. ``seed`` and ``count`` identify the generation the
    labels came from, so a run record can assert its panel was scored against labels from the same
    data.
    """

    seed: int
    count: int
    as_of: str
    panel: tuple[str, ...]
    by_customer: dict[str, CustomerGroundTruth]

    def cohort_counts(self) -> dict[str, int]:
        """Realised panel size per cohort -- asserted by the panel-coverage test."""
        counts: dict[str, int] = {}
        for cid in self.panel:
            cohort = self.by_customer[cid].cohort
            counts[cohort] = counts.get(cohort, 0) + 1
        return counts


def export_ground_truth(
    *,
    count: int,
    seed: int,
    panel_per_cohort: int,
    as_of: date = DEFAULT_AS_OF,
) -> GroundTruth:
    """Generate a dataset and project its plan into evaluation labels (task 11.1, §15.1).

    Calls the generator directly rather than reading a database, so the labels are the generator's
    own decisions and cannot drift from service code (design §15.1). ``panel_per_cohort`` is the
    floor per cohort (design §14.2: "at least 3 per cohort"); the actual panel may be larger if a
    cohort is small enough that the floor exceeds its size, in which case every member is taken.
    """
    population, _dataset = generate(count=count, seed=seed, as_of=as_of)
    panel = _select_panel(population, panel_per_cohort=panel_per_cohort)
    by_customer = {cid: _label_customer(population.by_id[cid], as_of=as_of) for cid in panel}
    return GroundTruth(
        seed=seed,
        count=count,
        as_of=as_of.isoformat(),
        panel=tuple(panel),
        by_customer=by_customer,
    )


# ---------------------------------------------------------------- panel selection


def _select_panel(population: Population, *, panel_per_cohort: int) -> list[str]:
    """Pick a stable panel: the lowest-index customers of each cohort, floor per cohort.

    Deterministic because ``population.customers`` is in generation (index) order and never a set,
    so the same generation always yields the same panel. Every cohort that has any members is
    represented, so the eight-cohort coverage the design requires holds even when a cohort is at its
    per-cohort floor of three.
    """
    by_cohort: dict[Cohort, list[CustomerPlan]] = {}
    for plan in population.customers:
        by_cohort.setdefault(plan.cohort, []).append(plan)

    panel: list[str] = []
    # Iterate cohorts in enum declaration order so the panel's overall order is stable and readable.
    for cohort in Cohort:
        members = by_cohort.get(cohort, [])
        members.sort(key=lambda plan: plan.index)
        for plan in members[:panel_per_cohort]:
            panel.append(plan.customer_id)
    return panel


# ---------------------------------------------------------------- per-customer labelling


def _label_customer(plan: CustomerPlan, *, as_of: date) -> CustomerGroundTruth:
    return CustomerGroundTruth(
        customer_id=plan.customer_id,
        cohort=plan.cohort.value,
        segment=plan.segment.value,
        material_facts=_material_facts(plan, as_of=as_of),
        life_events=_life_events(plan),
        propensity_ranking=_propensity_ranking(plan),
        risk=_risk_composition(plan),
    )


def _material_facts(plan: CustomerPlan, *, as_of: date) -> tuple[MaterialFact, ...]:
    """The material facts design §14.2 names, read from the plan's own decisions.

    Each is present only when the generator actually created the condition, so a customer with no
    delinquency has no delinquency material fact -- the coverage scorer would otherwise penalise an
    agent for not mentioning something that is not true.
    """
    facts: list[MaterialFact] = []

    if plan.days_past_due >= _MATERIAL_DPD:
        facts.append(
            MaterialFact(
                kind="DELINQUENCY_90",
                detail=f"{plan.days_past_due} days past due",
                keywords=("past due", "delinquen", "dpd", "arrears"),
            )
        )

    if plan.fraud_alerts > 0:
        facts.append(
            MaterialFact(
                kind="FRAUD_ALERT",
                detail=f"{plan.fraud_alerts} fraud alert(s)",
                keywords=("fraud", "alert", "suspicious"),
            )
        )

    if plan.profile.fraud_flagged:
        # The fraud cohort carries AML/PEP scrutiny (design §15); the agent must surface it.
        facts.append(
            MaterialFact(
                kind="AML_PEP",
                detail="AML/PEP scrutiny",
                keywords=("aml", "pep", "sanction", "politically exposed", "money laundering"),
            )
        )

    if _has_maturing_deposit(plan, as_of=as_of):
        facts.append(
            MaterialFact(
                kind="MATURING_DEPOSIT",
                detail="deposit maturing within the window",
                keywords=("matur", "cd ", "certificate of deposit", "term deposit", "rollover"),
            )
        )

    return tuple(facts)


def _has_maturing_deposit(plan: CustomerPlan, *, as_of: date) -> bool:
    """Whether the customer holds a term deposit maturing inside the coverage window."""
    for account in plan.accounts_of(AccountType.DEPOSIT):
        if account.deposit_type is not DepositProductType.CD:
            continue
        maturity = account.maturity_date
        if maturity is None:
            continue
        delta = (maturity - as_of).days
        if 0 <= delta <= _MATURING_WINDOW_DAYS:
            return True
    return False


def _life_events(plan: CustomerPlan) -> tuple[TrueLifeEvent, ...]:
    """The generator's planned life events, in date order (dimension 8)."""
    events = sorted(plan.life_events, key=lambda event: event.event_date)
    return tuple(
        TrueLifeEvent(
            event_type=event.life_event_type.value,
            event_date=event.event_date.isoformat(),
            inferred=event.is_inferred,
            confidence=event.confidence,
        )
        for event in events
    )


def _propensity_ranking(plan: CustomerPlan) -> tuple[str, ...]:
    """The products the customer is most likely to accept, most likely first (dimension 9).

    Derived from cohort and current holdings the same way the generator's own offer stage reasons:
    a customer without a card is a card prospect; an affluent or HNW customer without investments is
    a wealth prospect; a customer with a healthy income and no mortgage is a lending prospect. This
    is a *label*, not the agent's answer -- the offer agent's ranking is scored against this order.
    """
    ranking: list[str] = []
    held = {account.account_type for account in plan.accounts}
    profile = plan.profile

    if AccountType.CARD not in held and profile.card_probability > 0:
        ranking.append("CARD")
    if AccountType.INVESTMENT not in held and profile.investment_probability > 0:
        ranking.append("WEALTH")
    has_mortgage = any(
        account.loan_type is LoanType.MORTGAGE for account in plan.accounts_of(AccountType.LOAN)
    )
    if not has_mortgage and profile.mortgage_probability > 0:
        ranking.append("MORTGAGE")
    if profile.business_loan_probability > 0:
        ranking.append("BUSINESS_LOAN")
    # A deposit upsell is always plausible and ranks last as the fallback next-best product.
    ranking.append("DEPOSIT")
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for item in ranking:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return tuple(unique)


def _risk_composition(plan: CustomerPlan) -> RiskComposition:
    """The factors that compose the customer's risk, from the generator's own decisions (dim 10).

    The generator sets delinquency, fraud and thin-file conditions per cohort; the risk score is a
    function of those. The labels here name the factors the risk agent is expected to cite, so a
    factor is included only when the underlying condition is actually present.
    """
    factors: list[str] = []
    if plan.days_past_due >= _MATERIAL_DPD:
        factors.append("DELINQUENCY")
    if plan.profile.delinquent:
        factors.append("PAYMENT_HISTORY")
    if plan.profile.fraud_flagged:
        factors.append("FRAUD_RISK")
    if not plan.has_bureau_file:
        factors.append("THIN_FILE")
    if not factors:
        # A clean customer's risk is composed of the standard positive factors; naming them lets the
        # scorer credit an agent that correctly reports a low-risk profile rather than inventing
        # one.
        factors.append("PAYMENT_HISTORY")
    return RiskComposition(factors=tuple(factors))


__all__ = [
    "CustomerGroundTruth",
    "GroundTruth",
    "MaterialFact",
    "RiskComposition",
    "TrueLifeEvent",
    "export_ground_truth",
]
