"""Life-event planning (task 2.6, planning half).

Runs before :mod:`c360.generator.transactions` and after :mod:`c360.generator.products`, which is
the
only order that satisfies both halves of design §15's requirement that life events be seeded *with
corroborating transactions*:

* after products, so a ``HOME_PURCHASE`` can be dated to an actual mortgage's start date and a
  ``BUSINESS_START`` to an actual business loan, rather than asserting a purchase the holdings do
  not
  support;
* before transactions, so the transaction generator can emit the down payment, the insurance premium
  and the salary step-change that make the event inferable.

The rule that keeps the evaluation honest
-----------------------------------------

**An event is only marked ``INFERRED`` if it falls inside the transaction window.** Requirement 7.3
says an AI-inferred event must cite the signals it was inferred from, and migration ``0003``
enforces
that with ``CHECK (is_inferred = 0 OR (confidence IS NOT NULL AND signals IS NOT NULL))``. An event
dated before the 24-month window has no transactions to cite, so labelling it inferred would create
a
ground-truth row that no agent could ever legitimately derive — and design §14.3's precision and
recall for the Life Event Agent would then be measured against an impossible target. Events outside
the window are recorded as ``SYSTEM_OF_RECORD`` or ``CUSTOMER_DECLARED``, which need no signals.

Circumstantial events are preferred over random ones. A ``RETIREMENT`` for a customer whose
employment
status is ``RETIRED`` and a ``HOME_PURCHASE`` for a customer holding a mortgage are corroborated by
the
rest of the record, not only by transactions, which is what makes the panel in design §14.2 worth
scoring against.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from c360.domain.enums import AccountType, LifeEventSource, LifeEventType, LoanType
from c360.generator.context import GeneratorContext
from c360.generator.plan import CustomerPlan, PlannedLifeEvent, Population

#: Confidence bounds, in whole percent, for an inferred event. Converted to the 0..1 REAL the column
#: holds. Never 1.0: an inference presented as certainty is the thing requirement 7.3 exists to
#: prevent.
_INFERRED_CONFIDENCE_PERCENT: Final = (55, 92)

#: Source weights for an event that could plausibly have come from anywhere, when the event date
#: sits
#: inside the transaction window and inference is therefore legitimate.
_SOURCE_WEIGHTS_IN_WINDOW: Final[tuple[tuple[LifeEventSource, int], ...]] = (
    (LifeEventSource.INFERRED, 45),
    (LifeEventSource.CUSTOMER_DECLARED, 30),
    (LifeEventSource.SYSTEM_OF_RECORD, 25),
)

#: Source weights outside the window, where no transaction can corroborate the event.
_SOURCE_WEIGHTS_OUT_OF_WINDOW: Final[tuple[tuple[LifeEventSource, int], ...]] = (
    (LifeEventSource.SYSTEM_OF_RECORD, 60),
    (LifeEventSource.CUSTOMER_DECLARED, 40),
)

#: Age at which a retirement event becomes plausible.
_RETIREMENT_AGE: Final = 58

#: Events drawn at random when the circumstantial ones do not fill the cohort's quota.
_OPPORTUNISTIC_EVENTS: Final[tuple[tuple[LifeEventType, int], ...]] = (
    (LifeEventType.JOB_CHANGE, 24),
    (LifeEventType.PROMOTION, 20),
    (LifeEventType.RELOCATION, 16),
    (LifeEventType.EDUCATION, 12),
    (LifeEventType.CHILD_BIRTH, 10),
    (LifeEventType.MARRIAGE, 8),
    (LifeEventType.BEREAVEMENT, 5),
    (LifeEventType.DIVORCE, 5),
)


def _source_for(ctx: GeneratorContext, event_date: date) -> tuple[LifeEventSource, float | None]:
    """Choose a source, and a confidence when the source is inference."""
    in_window = event_date >= ctx.history_start
    weights = _SOURCE_WEIGHTS_IN_WINDOW if in_window else _SOURCE_WEIGHTS_OUT_OF_WINDOW
    source = ctx.weighted(weights)
    if source is LifeEventSource.INFERRED:
        return source, ctx.integer(_INFERRED_CONFIDENCE_PERCENT) / 100
    return source, None


def _event(
    ctx: GeneratorContext,
    event_type: LifeEventType,
    event_date: date,
    *,
    force_source: LifeEventSource | None = None,
) -> PlannedLifeEvent:
    """Build one planned event, clamping the date into the customer's plausible range."""
    clamped = min(event_date, ctx.as_of)
    if force_source is not None:
        confidence = (
            ctx.integer(_INFERRED_CONFIDENCE_PERCENT) / 100
            if force_source is LifeEventSource.INFERRED
            else None
        )
        return PlannedLifeEvent(
            life_event_type=event_type,
            event_date=clamped,
            source=force_source,
            confidence=confidence,
        )
    source, confidence = _source_for(ctx, clamped)
    return PlannedLifeEvent(
        life_event_type=event_type,
        event_date=clamped,
        source=source,
        confidence=confidence,
    )


def _circumstantial(ctx: GeneratorContext, plan: CustomerPlan) -> list[PlannedLifeEvent]:
    """Events the rest of the customer's record already implies.

    Each is anchored to the artefact that corroborates it — a mortgage, a business loan, a student
    loan, a retired employment status — and recorded as system-of-record, because the bank genuinely
    would know about it.
    """
    events: list[PlannedLifeEvent] = []

    for account in plan.accounts_of(AccountType.LOAN):
        start = account.loan_start_date or account.open_date
        if account.loan_type is LoanType.MORTGAGE:
            events.append(
                _event(
                    ctx,
                    LifeEventType.HOME_PURCHASE,
                    start,
                    force_source=LifeEventSource.SYSTEM_OF_RECORD,
                )
            )
        elif account.loan_type is LoanType.BUSINESS:
            events.append(
                _event(
                    ctx,
                    LifeEventType.BUSINESS_START,
                    start,
                    force_source=LifeEventSource.SYSTEM_OF_RECORD,
                )
            )
        elif account.loan_type is LoanType.STUDENT:
            events.append(
                _event(
                    ctx,
                    LifeEventType.EDUCATION,
                    start,
                    force_source=LifeEventSource.CUSTOMER_DECLARED,
                )
            )

    if plan.employment_status == "RETIRED" and plan.date_of_birth is not None:
        age = (ctx.as_of - plan.date_of_birth).days // 365
        if age >= _RETIREMENT_AGE:
            # Dated to roughly when they would have retired, inside the window where possible so the
            # salary-to-pension change in the transaction stream corroborates it.
            retirement = ctx.date_between(ctx.history_start, ctx.as_of)
            events.append(
                _event(
                    ctx,
                    LifeEventType.RETIREMENT,
                    retirement,
                    force_source=LifeEventSource.CUSTOMER_DECLARED,
                )
            )

    if plan.marital_status == "MARRIED" and ctx.chance(55):
        events.append(
            _event(
                ctx,
                LifeEventType.MARRIAGE,
                ctx.date_between(max(plan.customer_since, ctx.history_start), ctx.as_of),
            )
        )
    if plan.marital_status == "DIVORCED" and ctx.chance(60):
        events.append(
            _event(
                ctx,
                LifeEventType.DIVORCE,
                ctx.date_between(max(plan.customer_since, ctx.history_start), ctx.as_of),
            )
        )

    if plan.cohort.value == "HNW" and ctx.chance(30):
        events.append(
            _event(ctx, LifeEventType.INHERITANCE, ctx.date_between(ctx.history_start, ctx.as_of))
        )

    return events


def plan_life_events(ctx: GeneratorContext, population: Population) -> None:
    """Decide each customer's life events, in date order.

    The cohort's ``life_event_count`` is a target, not a guarantee: circumstantial events are always
    kept even when they exceed it, because dropping the ``HOME_PURCHASE`` of a customer who visibly
    holds a mortgage would contradict the holdings.
    """
    for plan in population.customers:
        events = _circumstantial(ctx, plan)
        target = ctx.integer(plan.profile.life_event_count)

        # Fill the remainder opportunistically, without repeating a type this customer already has.
        attempts = 0
        max_attempts = len(_OPPORTUNISTIC_EVENTS) * 2
        while len(events) < target and attempts < max_attempts:
            attempts += 1
            event_type = ctx.weighted(_OPPORTUNISTIC_EVENTS)
            if any(existing.life_event_type is event_type for existing in events):
                continue
            events.append(
                _event(
                    ctx,
                    event_type,
                    ctx.date_between(max(plan.customer_since, ctx.history_start), ctx.as_of),
                )
            )

        # Deduplicate by type, keeping the earliest occurrence, then order by date. The timeline in
        # requirement 7.1 is chronological, and two MARRIAGE rows for one customer would read as a
        # data-quality defect rather than as generated variety.
        seen: list[LifeEventType] = []
        deduplicated: list[PlannedLifeEvent] = []
        for event in sorted(events, key=lambda item: (item.event_date, item.life_event_type.value)):
            if event.life_event_type in seen:
                continue
            seen.append(event.life_event_type)
            deduplicated.append(event)

        plan.life_events = deduplicated
