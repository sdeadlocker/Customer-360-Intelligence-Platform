"""Applications, campaigns, offers, engagement events and life-event rows (task 2.6).

Four things here exist to make later phases testable rather than to add volume.

**Offers are suppressed for a real reason.** Requirement 9.5 suppresses an offer for a product the
customer already holds, shows it greyed, and displays the reason. That is only exercisable if the
offer
catalogue and the product catalogue share a key — so every offer carries a ``product_code`` drawn
from
:mod:`c360.generator.vocab`'s product tables, and suppression is decided by actually checking the
customer's holdings. Insurance offers deliberately carry codes no account can have, so the
not-suppressed path is equally real.

**Declined offers carry a reaction date inside the cooling-off window.** Requirement 9.6 forbids
re-ranking a declined offer into the top position for a configurable number of days
(``OFFER_COOLING_OFF_DAYS``, default 90). Some declines are therefore dated inside 90 days of the
as-of
date and some outside it, so Phase 5.5 has both a suppressed-by-cooling-off case and an
expired-window
case to distinguish.

**Expected value is computed, not drawn.** Requirement 9.2 ranks by expected value and requires a
rationale. ``expected_value_cents = value_cents * acceptance_probability``, evaluated with
:meth:`c360.domain.money.Cents.apply_bps`, so the number the UI ranks on is reproducible from the
two
numbers next to it. Drawing it independently would let the ranking contradict its own explanation.

**The fraud cohort's applications actually fail.** Design §15 gives that cohort "fraud alerts and
failed
applications", so its applications are ``DECLINED`` with ``fraud_result = 'FAIL'``. The schema pairs
a
decision with a decision date — ``CHECK ((application_status IN ('APPROVED','DECLINED','FUNDED')) =
(decision_date IS NOT NULL))`` — so a pending application must not have one, and a decided one must.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Final

from c360.domain.enums import (
    ApplicationStatus,
    BusinessGroup,
    CampaignStatus,
    CustomerReaction,
    EngagementChannel,
    EngagementEventType,
    EngagementOutcome,
    FraudResult,
    LifeEventSource,
    OfferStatus,
    OfferType,
    ProbabilityConfidence,
)
from c360.domain.money import Bps, Cents
from c360.generator import vocab
from c360.generator.adversarial import adversarial_service_note
from c360.generator.context import SOURCE_LOS, GeneratorContext, add_months
from c360.generator.plan import CustomerPlan, Population
from c360.generator.tables import (
    APPLICATION,
    CAMPAIGN,
    CUSTOMER_EVENT,
    CUSTOMER_OFFER,
    LIFE_EVENT,
    OFFER,
    Dataset,
)

#: The offer catalogue, as
#: ``(offer_name, business_group, offer_type, product_code, product_type, value cents bounds)``.
#:
#: ``product_code`` is the join to the customer's holdings that makes requirement 9.5's
#: duplicate-product suppression real. The insurance entries use codes that no ``account`` row can
#: carry, so they are never suppressed — which keeps the un-suppressed path in the same test.
_OFFER_CATALOG: Final[
    tuple[tuple[str, BusinessGroup, OfferType, str, str, tuple[int, int]], ...]
] = (
    (
        "High Yield Savings Upgrade",
        BusinessGroup.DEPOSITS,
        OfferType.UPSELL,
        "SAV-HIYIELD",
        "SAVINGS",
        (8_000, 45_000),
    ),
    (
        "Money Market Select Transfer",
        BusinessGroup.DEPOSITS,
        OfferType.CROSS_SELL,
        "MMA-SELECT",
        "MMA",
        (12_000, 60_000),
    ),
    (
        "12 Month Certificate Promotion",
        BusinessGroup.DEPOSITS,
        OfferType.CROSS_SELL,
        "CD-12M",
        "CD",
        (15_000, 90_000),
    ),
    (
        "Premier Checking Upgrade",
        BusinessGroup.DEPOSITS,
        OfferType.UPSELL,
        "DDA-PREMIER",
        "CHECKING",
        (4_000, 24_000),
    ),
    (
        "Mortgage Refinance Review",
        BusinessGroup.LENDING,
        OfferType.RETENTION,
        "MTG-30F",
        "MORTGAGE",
        (180_000, 1_400_000),
    ),
    (
        "Home Equity Line Pre-Approval",
        BusinessGroup.LENDING,
        OfferType.CROSS_SELL,
        "HELOC-VAR",
        "HELOC",
        (90_000, 700_000),
    ),
    (
        "Personal Loan Consolidation",
        BusinessGroup.LENDING,
        OfferType.CROSS_SELL,
        "PL-UNSEC",
        "PERSONAL",
        (40_000, 280_000),
    ),
    (
        "New Vehicle Financing",
        BusinessGroup.LENDING,
        OfferType.ACQUISITION,
        "AUTO-NEW",
        "AUTO",
        (35_000, 220_000),
    ),
    (
        "Cashback Plus Card",
        BusinessGroup.CARDS,
        OfferType.CROSS_SELL,
        "CC-CASHBACK",
        "CARD",
        (20_000, 120_000),
    ),
    (
        "Travel Elite Card Upgrade",
        BusinessGroup.CARDS,
        OfferType.UPSELL,
        "CC-TRAVEL",
        "CARD",
        (45_000, 260_000),
    ),
    (
        "Private Client Card Invitation",
        BusinessGroup.CARDS,
        OfferType.UPSELL,
        "CC-PRIVATE",
        "CARD",
        (150_000, 900_000),
    ),
    (
        "Managed Portfolio Consultation",
        BusinessGroup.WEALTH,
        OfferType.CROSS_SELL,
        "INV-MANAGED",
        "INVESTMENT",
        (200_000, 2_600_000),
    ),
    (
        "Retirement Account Rollover",
        BusinessGroup.WEALTH,
        OfferType.CROSS_SELL,
        "INV-IRA",
        "INVESTMENT",
        (120_000, 1_100_000),
    ),
    (
        "Education Savings Plan",
        BusinessGroup.WEALTH,
        OfferType.CROSS_SELL,
        "INV-529",
        "INVESTMENT",
        (30_000, 240_000),
    ),
    (
        "Term Life Protection Review",
        BusinessGroup.INSURANCE,
        OfferType.CROSS_SELL,
        "INS-TERMLIFE",
        "INSURANCE",
        (25_000, 180_000),
    ),
    (
        "Property Insurance Bundle",
        BusinessGroup.INSURANCE,
        OfferType.CROSS_SELL,
        "INS-PROPERTY",
        "INSURANCE",
        (18_000, 140_000),
    ),
    (
        "Business Term Loan Offer",
        BusinessGroup.BUSINESS,
        OfferType.CROSS_SELL,
        "BUS-TERM",
        "BUSINESS",
        (150_000, 1_800_000),
    ),
    (
        "Merchant Services Onboarding",
        BusinessGroup.BUSINESS,
        OfferType.SERVICE,
        "BUS-MERCH",
        "SERVICE",
        (20_000, 160_000),
    ),
    (
        "Digital Banking Activation",
        BusinessGroup.DEPOSITS,
        OfferType.SERVICE,
        "SVC-DIGITAL",
        "SERVICE",
        (2_000, 12_000),
    ),
    (
        "Relationship Pricing Review",
        BusinessGroup.WEALTH,
        OfferType.RETENTION,
        "SVC-PRICING",
        "SERVICE",
        (30_000, 300_000),
    ),
)

#: Offers presented per customer.
_OFFERS_PER_CUSTOMER: Final = (2, 6)

#: Reaction distribution. ``PENDING`` dominates because most live offers have not been acted on, and
#: the schema pairs ``PENDING`` with a NULL reaction date.
_REACTIONS: Final[tuple[tuple[CustomerReaction, int], ...]] = (
    (CustomerReaction.PENDING, 38),
    (CustomerReaction.VIEWED, 18),
    (CustomerReaction.IGNORED, 14),
    (CustomerReaction.INTERESTED, 12),
    (CustomerReaction.DECLINED, 11),
    (CustomerReaction.ACCEPTED, 7),
)

#: Acceptance probability bounds, in percent, by value tier. A platinum customer is a better
#: prospect,
#: which is what makes the expected-value ranking in requirement 9.2 order differently per customer.
_ACCEPTANCE_PERCENT: Final[dict[str, tuple[int, int]]] = {
    "BRONZE": (2, 18),
    "SILVER": (5, 28),
    "GOLD": (10, 42),
    "PLATINUM": (16, 58),
}

#: Cooling-off window from the settings default, used to place some declines inside it and some
#: outside. The service layer reads the configured value; this only has to straddle it.
_COOLING_OFF_DAYS: Final = 90

#: Engagement events per customer over the window. ~15 at 100 customers is design §15's ~1.5k.
_EVENTS_PER_CUSTOMER: Final = (9, 22)

#: Event types with weights, and the channel each one implies. A ``BRANCH_VISIT`` over ``MOBILE`` is
#: the kind of contradiction that makes a filtered engagement view untrustworthy.
_EVENT_TYPES: Final[tuple[tuple[EngagementEventType, int, tuple[EngagementChannel, ...]], ...]] = (
    (EngagementEventType.LOGIN, 30, (EngagementChannel.WEB, EngagementChannel.MOBILE)),
    (EngagementEventType.APP_USAGE, 16, (EngagementChannel.MOBILE,)),
    (EngagementEventType.STATEMENT_VIEW, 11, (EngagementChannel.WEB, EngagementChannel.MOBILE)),
    (EngagementEventType.OTP_REQUEST, 8, (EngagementChannel.SMS, EngagementChannel.EMAIL)),
    (EngagementEventType.OTP_VERIFY, 7, (EngagementChannel.SMS, EngagementChannel.EMAIL)),
    (EngagementEventType.LOGOUT, 6, (EngagementChannel.WEB, EngagementChannel.MOBILE)),
    (EngagementEventType.BRANCH_VISIT, 5, (EngagementChannel.BRANCH,)),
    (EngagementEventType.CALL, 5, (EngagementChannel.CALL_CENTER,)),
    (EngagementEventType.PASSWORD_RESET, 4, (EngagementChannel.WEB, EngagementChannel.EMAIL)),
    (EngagementEventType.CHAT, 3, (EngagementChannel.WEB, EngagementChannel.MOBILE)),
    (
        EngagementEventType.SERVICE_REQUEST,
        3,
        (EngagementChannel.CALL_CENTER, EngagementChannel.WEB),
    ),
    (EngagementEventType.COMPLAINT, 2, (EngagementChannel.CALL_CENTER, EngagementChannel.BRANCH)),
)

#: Event types that carry free-text notes.
_NOTED_EVENTS: Final = (
    EngagementEventType.SERVICE_REQUEST,
    EngagementEventType.COMPLAINT,
)

#: Channels that carry a session identifier. A branch visit has no session.
_SESSION_CHANNELS: Final = (
    EngagementChannel.WEB,
    EngagementChannel.MOBILE,
)

#: Applications per customer.
_APPLICATIONS_PER_CUSTOMER: Final = (0, 3)

#: Application channels.
_APPLICATION_CHANNELS: Final[tuple[tuple[str, int], ...]] = (
    ("ONLINE", 44),
    ("MOBILE", 26),
    ("BRANCH", 18),
    ("CALL_CENTER", 12),
)

#: Percent of the fraud cohort's applications that are declined on a failed fraud check.
_FRAUD_DECLINE_PERCENT: Final = 70


def _emit_campaigns(ctx: GeneratorContext, dataset: Dataset) -> list[str]:
    """Emit the campaign table and return the campaign IDs, in order."""
    campaign_ids: list[str] = []
    for index, (name, business_group) in enumerate(vocab.CAMPAIGNS, start=1):
        campaign_id = ctx.entity_id("CMP", index, width=4)
        start = ctx.date_between(ctx.history_start, add_months(ctx.as_of, -1))
        end = add_months(start, ctx.integer((3, 14)))
        # CHECK (end_date >= start_date), and a campaign that ended is COMPLETED rather than ACTIVE.
        status = (
            CampaignStatus.ACTIVE
            if end >= ctx.as_of
            else ctx.weighted(((CampaignStatus.COMPLETED, 80), (CampaignStatus.CANCELLED, 20)))
        )
        dataset.add(
            CAMPAIGN,
            (
                campaign_id,
                name,
                business_group,
                ctx.weighted(
                    (("EMAIL", 38), ("MOBILE", 24), ("BRANCH", 18), ("SMS", 12), ("CALL_CENTER", 8))
                ),
                start.isoformat(),
                end.isoformat(),
                status.value,
                ctx.as_of_iso,
            ),
        )
        campaign_ids.append(campaign_id)
    return campaign_ids


def _emit_offers(
    ctx: GeneratorContext, dataset: Dataset, campaign_ids: list[str]
) -> list[tuple[str, str, str, Cents]]:
    """Emit the offer catalogue.

    Returns ``(offer_id, product_code, product_type, value)`` per offer, which is what the
    per-customer stage needs to decide suppression and expected value.
    """
    catalog: list[tuple[str, str, str, Cents]] = []
    for index, entry in enumerate(_OFFER_CATALOG, start=1):
        name, business_group, offer_type, product_code, product_type, value_bounds = entry
        offer_id = ctx.entity_id("OF", index, width=4)
        value = ctx.cents(value_bounds, round_to=100)
        start = ctx.date_between(ctx.history_start, add_months(ctx.as_of, -1))
        end = add_months(start, ctx.integer((2, 18)))
        status = OfferStatus.ACTIVE if end >= ctx.as_of else OfferStatus.EXPIRED

        dataset.add(
            OFFER,
            (
                offer_id,
                name,
                business_group.value,
                offer_type.value,
                product_code,
                product_type,
                int(value),
                start.isoformat(),
                end.isoformat(),
                status.value,
                ctx.pick(campaign_ids) if ctx.chance(75) else None,
                ctx.as_of_iso,
            ),
        )
        catalog.append((offer_id, product_code, product_type, value))
    return catalog


def _held_product_codes(plan: CustomerPlan) -> list[str]:
    """Product codes the customer already holds, for duplicate-product suppression."""
    return [account.product_code for account in plan.accounts if account.is_open]


def _emit_customer_offers(
    ctx: GeneratorContext,
    plan: CustomerPlan,
    dataset: Dataset,
    catalog: list[tuple[str, str, str, Cents]],
    sequence_start: int,
) -> int:
    """Present a selection of offers to one customer. Returns the next sequence number."""
    sequence = sequence_start
    if not plan.profile.has_offers:
        return sequence

    held = _held_product_codes(plan)
    count = ctx.integer(_OFFERS_PER_CUSTOMER)
    # sample() so one customer never gets the same offer twice; the table has
    # UNIQUE (customer_id, offer_id).
    for offer_id, product_code, product_type, value in ctx.sample(catalog, count):
        sequence += 1
        presented = ctx.date_between(ctx.history_start, ctx.as_of)

        reaction = ctx.weighted(_REACTIONS)
        if reaction is CustomerReaction.PENDING:
            reaction_date: date | None = None
        elif reaction is CustomerReaction.DECLINED:
            # Straddle the cooling-off window deliberately: requirement 9.6 needs both sides.
            inside = ctx.chance(55)
            earliest = (
                ctx.as_of - timedelta(days=_COOLING_OFF_DAYS - 1)
                if inside
                else ctx.as_of - timedelta(days=_COOLING_OFF_DAYS * 3)
            )
            latest = ctx.as_of if inside else ctx.as_of - timedelta(days=_COOLING_OFF_DAYS + 1)
            # A reaction can never precede the presentation, whichever side of the window it is on.
            window_start = max(presented, min(earliest, latest))
            window_end = max(window_start, earliest, latest)
            reaction_date = ctx.date_between(window_start, window_end)
        else:
            reaction_date = ctx.date_between(presented, ctx.as_of)

        low, high = _ACCEPTANCE_PERCENT[plan.value_tier.value]
        probability_percent = ctx.integer((low, high))
        probability = probability_percent / 100
        confidence = (
            ProbabilityConfidence.HIGH
            if probability_percent >= high - (high - low) // 3
            else (
                ProbabilityConfidence.LOW
                if probability_percent <= low + (high - low) // 3
                else ProbabilityConfidence.MEDIUM
            )
        )
        # Requirement 9.2: expected value is the offer's worth times the acceptance probability.
        expected_value = value.apply_bps(Bps(probability_percent * 100))

        is_suppressed = product_code in held
        suppression_reason = (
            f"DUPLICATE_PRODUCT: customer already holds {product_code}" if is_suppressed else None
        )

        dataset.add(
            CUSTOMER_OFFER,
            (
                ctx.entity_id("CO", sequence),
                plan.customer_id,
                offer_id,
                presented.isoformat(),
                reaction.value,
                reaction_date.isoformat() if reaction_date is not None else None,
                probability,
                confidence.value,
                int(expected_value),
                f"PRODUCT_AFFINITY:{product_type}",
                1 if is_suppressed else 0,
                suppression_reason,
                ctx.weighted((("EMAIL", 40), ("MOBILE", 28), ("BRANCH", 18), ("CALL_CENTER", 14))),
                ctx.as_of_iso,
            ),
        )
    return sequence


def _emit_applications(
    ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset, sequence_start: int
) -> int:
    """Emit product applications, including the fraud cohort's failures."""
    sequence = sequence_start
    count = ctx.integer(_APPLICATIONS_PER_CUSTOMER)
    if plan.profile.fraud_flagged:
        count = max(count, 2)

    accounts = plan.accounts
    for _ in range(count):
        sequence += 1
        applied = ctx.date_between(ctx.history_start, ctx.as_of)
        requested = ctx.cents((200_000, 40_000_000), round_to=10_000)

        if plan.profile.fraud_flagged and ctx.chance(_FRAUD_DECLINE_PERCENT):
            status = ApplicationStatus.DECLINED
            fraud_result = FraudResult.FAIL
        else:
            status = ctx.weighted(
                (
                    (ApplicationStatus.FUNDED, 34),
                    (ApplicationStatus.APPROVED, 22),
                    (ApplicationStatus.DECLINED, 16),
                    (ApplicationStatus.SUBMITTED, 12),
                    (ApplicationStatus.IN_REVIEW, 10),
                    (ApplicationStatus.WITHDRAWN, 6),
                )
            )
            fraud_result = ctx.weighted(
                ((FraudResult.PASS, 88), (FraudResult.REVIEW, 9), (FraudResult.FAIL, 3))
            )

        # CHECK ((status IN ('APPROVED','DECLINED','FUNDED')) = (decision_date IS NOT NULL)) and
        # CHECK (decision_date >= event_date).
        decision_date = ctx.date_between(applied, ctx.as_of) if status.is_decided else None
        approved_amount = (
            requested if status in (ApplicationStatus.APPROVED, ApplicationStatus.FUNDED) else None
        )
        # Only a funded application produced a product, so only it links to an account.
        linked_account = (
            ctx.pick(accounts).account_id
            if status is ApplicationStatus.FUNDED and accounts
            else None
        )
        product = ctx.pick(vocab.LOAN_PRODUCTS)

        dataset.add(
            APPLICATION,
            (
                ctx.entity_id("AP", sequence),
                plan.customer_id,
                product[1],
                product[0],
                applied.isoformat(),
                ctx.weighted(_APPLICATION_CHANNELS),
                status.value,
                fraud_result.value,
                decision_date.isoformat() if decision_date is not None else None,
                int(requested),
                int(approved_amount) if approved_amount is not None else None,
                linked_account,
                ctx.as_of_iso,
                SOURCE_LOS,
            ),
        )
    return sequence


def _emit_engagement(
    ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset, sequence_start: int
) -> int:
    """Emit engagement events across channels (requirement 7.6)."""
    sequence = sequence_start
    count = ctx.integer(_EVENTS_PER_CUSTOMER)
    type_options = [(entry[0], entry[1]) for entry in _EVENT_TYPES]
    channels_for = {entry[0]: entry[2] for entry in _EVENT_TYPES}

    for _ in range(count):
        sequence += 1
        event_type = ctx.weighted(type_options)
        channel = ctx.pick(channels_for[event_type])
        when = ctx.date_between(max(plan.customer_since, ctx.history_start), ctx.as_of)

        outcome = ctx.weighted(
            (
                (EngagementOutcome.SUCCESS, 84),
                (EngagementOutcome.FAILURE, 9),
                (EngagementOutcome.ABANDONED, 5),
                (EngagementOutcome.PENDING, 2),
            )
        )
        notes: str | None = None
        if event_type in _NOTED_EVENTS:
            # A small number of these carry the adversarial payload of task 2.7.
            notes = adversarial_service_note(ctx, plan) or ctx.pick(vocab.SERVICE_NOTES)

        dataset.add(
            CUSTOMER_EVENT,
            (
                f"{plan.customer_id}-EV-{sequence:06d}",
                plan.customer_id,
                event_type.value,
                when.isoformat(),
                channel.value,
                f"S-{ctx.digits(12)}" if channel in _SESSION_CHANNELS else None,
                ctx.weighted(vocab.DEVICE_TYPES),
                outcome.value,
                notes,
                ctx.as_of_iso,
            ),
        )
    return sequence


def _emit_life_events(ctx: GeneratorContext, plan: CustomerPlan, dataset: Dataset) -> None:
    """Emit the planned life events, enforcing the inferred/evidence pairing.

    An inferred event with no signals is downgraded to ``CUSTOMER_DECLARED`` rather than written.
    Migration ``0003`` would accept ``'[]'`` — it is valid JSON and non-NULL — but requirement 7.3
    requires an inferred event to cite what it was inferred from, and an empty citation list is a
    ground-truth row no agent could ever justify producing.
    """
    for ordinal, event in enumerate(plan.life_events, start=1):
        source = event.source
        confidence = event.confidence
        if source is LifeEventSource.INFERRED and not event.signals:
            source = LifeEventSource.CUSTOMER_DECLARED
            confidence = None

        is_inferred = source is LifeEventSource.INFERRED
        dataset.add(
            LIFE_EVENT,
            (
                f"{plan.customer_id}-LE-{ordinal:02d}",
                plan.customer_id,
                event.life_event_type.value,
                event.event_date.isoformat(),
                confidence,
                source.value,
                1 if is_inferred else 0,
                # Signals are kept for every source, not only inferred ones: they are useful
                # evidence
                # on a declared event too, and the column is nullable rather than conditional.
                json.dumps(event.signals, separators=(",", ":")) if event.signals else None,
                ctx.as_of_iso,
            ),
        )


def generate_events(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    """Emit campaigns, offers, per-customer offers, applications, engagement and life events."""
    campaign_ids = _emit_campaigns(ctx, dataset)
    catalog = _emit_offers(ctx, dataset, campaign_ids)

    offer_sequence = 0
    application_sequence = 0
    event_sequence = 0
    for plan in population.customers:
        offer_sequence = _emit_customer_offers(ctx, plan, dataset, catalog, offer_sequence)
        application_sequence = _emit_applications(ctx, plan, dataset, application_sequence)
        event_sequence = _emit_engagement(ctx, plan, dataset, event_sequence)
        _emit_life_events(ctx, plan, dataset)
