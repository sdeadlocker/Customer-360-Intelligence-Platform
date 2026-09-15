"""Customers, contacts, employers and household formation (task 2.2).

Three decisions in here are worth explaining, because each one exists to make a later phase
non-trivial rather than to make this stage tidy.

**Households are formed before identities are filled in, not after.** Design §4.3 derives households
from address plus relationships, so members have to *share* an address — which means the address
belongs to the household and is copied down to the member, not drawn per customer and reconciled
later. Family names are shared the same way, with a deliberate minority of mixed-name households so
that name-based household inference cannot be assumed correct by anything downstream.

**Age drives household role, not the reverse.** Roles are assigned by sorting a household's members
by date of birth: oldest is ``HEAD``, a second adult within a plausible age band is ``SPOUSE`` or
``PARTNER``, and a member at least :data:`_GENERATION_GAP_YEARS` younger than the head is ``CHILD``.
Drawing the role first and then inventing an age to fit produces households whose children are older
than their parents, which reads as a bug the first time anyone opens the relationship graph.

**Sparse fields are real absences, not blanks.** The thin-file cohort gets ``NULL`` for date of
birth,
occupation, employer and marital status, and a minority of its members get no ``contact_info`` row
at
all. Requirement 4.7 distinguishes "no value" from zero or empty, and it can only be tested against
data where the distinction exists. Writing an empty string instead would satisfy the schema and
quietly remove the test case.
"""

from __future__ import annotations

import unicodedata
from datetime import date, timedelta
from typing import Final

from c360.domain.enums import CustomerType, HouseholdRole
from c360.domain.money import Cents
from c360.generator import vocab
from c360.generator.adversarial import (
    ADVERSARIAL_EMPLOYER_ID,
    adversarial_employer_row,
    is_probe_carrier_index,
)
from c360.generator.cohorts import COHORT_PROFILES, CohortProfile, allocate
from c360.generator.context import GeneratorContext, add_months, stable_hash
from c360.generator.plan import CustomerPlan, HouseholdPlan, Population

#: Household size distribution, as ``(size, weight)``. Calibrated so that ~100 customers form ~35
#: households, which is design §15's stated volume for the default dataset. Single-member households
#: are included because they are a distinct UI state from the isolated cohort: a household exists
#: and
#: has rollups, it simply has one member.
_HOUSEHOLD_SIZES: Final[tuple[tuple[int, int], ...]] = (
    (1, 12),
    (2, 38),
    (3, 27),
    (4, 15),
    (5, 8),
)

#: Probability, in percent, that a household member shares the household family name.
_SHARED_FAMILY_NAME_PERCENT: Final = 85

#: Age bands per cohort, in years, as ``(low, high)``. Applied at the as-of date.
_AGE_BANDS: Final[dict[str, tuple[int, int]]] = {
    "MASS_MARKET": (22, 70),
    "AFFLUENT": (30, 66),
    "HNW": (40, 80),
    "SMALL_BUSINESS": (28, 65),
    "THIN_FILE": (18, 26),
    "DELINQUENT": (22, 60),
    "FRAUD_FLAGGED": (22, 55),
    "ISOLATED": (25, 70),
}

#: Youngest age at which a customer can have opened an account.
_MIN_ACCOUNT_AGE: Final = 18

#: Minimum age difference for a household member to be treated as a child of the head.
_GENERATION_GAP_YEARS: Final = 16

#: Largest age gap between two adults still treated as a couple.
_COUPLE_AGE_SPREAD: Final = 14

#: Marital statuses that justify a ``SPOUSE`` rather than ``PARTNER`` role.
_MARRIED_STATUSES: Final[tuple[str, ...]] = ("MARRIED", "WIDOWED")

#: Percent of thin-file customers with no ``contact_info`` row at all.
_NO_CONTACT_ROW_PERCENT: Final = 35

#: Percent of customers living in an addressed unit (flat, suite) rather than a whole property.
_UNIT_ADDRESS_PERCENT: Final = 22

#: Value-score bounds per value tier, so ``customer_value`` and ``customer_value_score`` agree.
_VALUE_SCORE_BANDS: Final[dict[str, tuple[int, int]]] = {
    "BRONZE": (5, 35),
    "SILVER": (30, 60),
    "GOLD": (55, 85),
    "PLATINUM": (80, 100),
}


def _ascii_slug(text: str) -> str:
    """Fold ``text`` to lowercase ASCII for use in an email local part.

    The name pools contain diacritics and particles. An email address built from them verbatim would
    be invalid, and normalizing at the point of use rather than restricting the name pool keeps the
    names representative — which is the reason for the wide pool in the first place.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char for char in stripped.lower() if char.isalnum())


def _build_assignments(ctx: GeneratorContext) -> list[CohortProfile]:
    """Expand the cohort allocation into a per-customer, interleaved list of profiles.

    Interleaved rather than grouped so that consecutive customer IDs are not all the same cohort.
    Contiguous cohorts would make every list, search result and pagination page in development show
    one persona, and the ``LIMIT``-shaped bugs that only appear on a heterogeneous page would go
    unseen.
    """
    allocation = allocate(ctx.customer_count)
    assignments: list[CohortProfile] = []
    for profile in COHORT_PROFILES:
        assignments.extend([profile] * allocation[profile.cohort])
    return ctx.shuffled(assignments)


def _form_household_groups(
    ctx: GeneratorContext, assignments: list[CohortProfile]
) -> list[list[int]]:
    """Partition household-eligible customer indices into household groups.

    Returns groups of indices into ``assignments``. The isolated cohort is excluded entirely —
    design
    §15 gives it no household — so those indices appear in no group and keep ``household_id`` NULL.
    """
    eligible = [index for index, profile in enumerate(assignments) if profile.has_household]
    shuffled = ctx.shuffled(eligible)

    groups: list[list[int]] = []
    cursor = 0
    while cursor < len(shuffled):
        size = ctx.weighted(_HOUSEHOLD_SIZES)
        group = shuffled[cursor : cursor + size]
        cursor += size
        groups.append(sorted(group))
    return groups


def _employers(ctx: GeneratorContext) -> list[tuple[str, str, str, str, str]]:
    """Build the employer reference table.

    Each employer is given a city from the shared pool so that a shared employer is a usable
    relationship-inference basis in requirement 6.6 without also implying a shared address.
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for index, (name, industry) in enumerate(vocab.EMPLOYERS, start=1):
        city, state, _ = ctx.pick(vocab.CITIES)
        rows.append((ctx.entity_id("E", index, width=4), name, industry, city, state))
    # Task 2.7: one employer name carries instruction-like text, so the Phase 11 suite has a
    # data-borne injection payload that reaches a prompt through the customer profile.
    rows.append(adversarial_employer_row())
    return rows


def _draw_dates(ctx: GeneratorContext, profile: CohortProfile) -> tuple[date | None, date]:
    """Draw a date of birth and a customer-since date that are consistent with each other.

    Tenure is capped so that nobody opens an account before turning :data:`_MIN_ACCOUNT_AGE`.
    Without
    the cap the HNW cohort's 30-year tenure combined with a 40-year-old customer produces a
    ten-year-old account holder, and the timeline widget then shows a product acquired in primary
    school.
    """
    low, high = _AGE_BANDS[profile.cohort.value]
    age = ctx.integer((low, high))

    # Anchored to the as-of date and jittered *backwards only*, so the customer is genuinely at
    # least
    # `age` years old. Drawing a birth year and then a random month and day does not achieve that:
    # an
    # 18-year-old born in November is 17 on a September as-of date, and the tenure cap computed from
    # `age` is then one year too generous.
    anchor = date(ctx.as_of.year - age, ctx.as_of.month, min(ctx.as_of.day, 28))
    date_of_birth = anchor - timedelta(days=ctx.integer((0, 364)))
    eighteenth = add_months(date_of_birth, 12 * _MIN_ACCOUNT_AGE)

    # Tenure is a target; the 18th birthday is a hard floor. Clamping here rather than capping the
    # tenure beforehand keeps the two concerns separate and cannot be defeated by day-level jitter.
    tenure = ctx.integer(profile.tenure_years)
    target = add_months(ctx.as_of, -12 * tenure)
    customer_since = min(max(target, eighteenth), ctx.as_of)
    # A little day-level variety, bounded so it cannot cross either limit.
    slack = min(89, (ctx.as_of - customer_since).days)
    if slack > 0:
        customer_since = customer_since + timedelta(days=ctx.integer((0, slack)))

    return (None if profile.sparse_fields else date_of_birth), customer_since


def _draw_contact(
    ctx: GeneratorContext,
    plan_index: int,
    given_name: str,
    family_name: str,
    profile: CohortProfile,
) -> tuple[str | None, str | None, str | None]:
    """Draw email, landline and mobile.

    ``.invalid`` is reserved by RFC 2606 and ``555-01xx`` is the fictional-use line range, so
    nothing
    generated here can reach a real address or number. The index is folded into the local part
    because
    the name pools are small enough to repeat, and a duplicate email would break the uniqueness the
    Phase 3.3 search index assumes for an identifier key.
    """
    local = f"{_ascii_slug(given_name)}.{_ascii_slug(family_name)}{plan_index}"
    email = f"{local}@example.invalid"
    area = ctx.pick(vocab.AREA_CODES)
    line = ctx.integer((100, 199))
    phone = f"+1-{area}-555-0{line}"
    mobile_area = ctx.pick(vocab.AREA_CODES)
    mobile_line = ctx.integer((100, 199))
    mobile = f"+1-{mobile_area}-555-0{mobile_line}"

    if profile.sparse_fields:
        # A thin file frequently has an email and nothing else.
        return email, None, mobile if ctx.chance(40) else None
    return email, phone, mobile


def plan_people(ctx: GeneratorContext) -> Population:
    """Build the population: employers, households, customers and their contact details.

    This is the first planning pass. Nothing is emitted; the returned :class:`Population` is what
    every later stage reads and extends.
    """
    population = Population()
    population.employers = _employers(ctx)
    # The adversarial employer is excluded from the normal pool and linked only to probe carriers,
    # so
    # the payload stays as sparse as task 2.7 requires instead of spreading across the population.
    employer_ids = [row[0] for row in population.employers if row[0] != ADVERSARIAL_EMPLOYER_ID]

    assignments = _build_assignments(ctx)
    groups = _form_household_groups(ctx, assignments)

    # Index -> household, so a customer being built knows the address and family name it inherits.
    household_of: dict[int, HouseholdPlan] = {}
    for ordinal, group in enumerate(groups, start=1):
        city, state, postal_prefix = ctx.pick(vocab.CITIES)
        family_name = ctx.pick(vocab.FAMILY_NAMES)
        street = f"{ctx.integer((100, 9899))} {ctx.pick(vocab.STREET_NAMES)}"
        postal = f"{postal_prefix}{ctx.integer((10, 99))}"
        formed = HouseholdPlan(
            household_id=ctx.entity_id("H", ordinal, width=4),
            name=ctx.pick(vocab.HOUSEHOLD_NAME_FORMS).format(family=family_name),
            address_hash=stable_hash(street, city, state, postal),
            city=city,
            state=state,
            postal_code=postal,
            street_address=street,
        )
        population.households.append(formed)
        for index in group:
            household_of[index] = formed

    for index, profile in enumerate(assignments):
        customer_id = ctx.entity_id("C", index + 1)
        household = household_of.get(index)

        if household is not None and ctx.chance(_SHARED_FAMILY_NAME_PERCENT):
            family_name = household.name.replace("The ", "").split(" ")[0]
        else:
            family_name = ctx.pick(vocab.FAMILY_NAMES)
        given_name = ctx.pick(vocab.GIVEN_NAMES)

        if household is not None:
            city, state, postal = household.city, household.state, household.postal_code
            street = household.street_address
        else:
            city, state, postal_prefix = ctx.pick(vocab.CITIES)
            postal = f"{postal_prefix}{ctx.integer((10, 99))}"
            street = f"{ctx.integer((100, 9899))} {ctx.pick(vocab.STREET_NAMES)}"

        date_of_birth, customer_since = _draw_dates(ctx, profile)
        segment = ctx.weighted(profile.segments)
        value_tier = ctx.weighted(profile.value_tiers)
        score_low, score_high = _VALUE_SCORE_BANDS[value_tier.value]

        is_business = profile.cohort.value == "SMALL_BUSINESS"
        occupation = (
            None
            if profile.sparse_fields
            else ctx.pick(vocab.PROPRIETOR_OCCUPATIONS if is_business else vocab.OCCUPATIONS)
        )
        employment_status = (
            None if profile.sparse_fields else ctx.weighted(vocab.EMPLOYMENT_STATUSES)
        )
        marital_status = None if profile.sparse_fields else ctx.weighted(vocab.MARITAL_STATUSES)

        email, phone, mobile = _draw_contact(ctx, index + 1, given_name, family_name, profile)
        unit = (
            f"{ctx.pick(vocab.UNIT_FORMS)} {ctx.integer((1, 48))}"
            if not profile.sparse_fields and ctx.chance(_UNIT_ADDRESS_PERCENT)
            else None
        )
        if profile.sparse_fields and ctx.chance(_NO_CONTACT_ROW_PERCENT):
            email, phone, mobile = None, None, None

        plan = CustomerPlan(
            customer_id=customer_id,
            index=index + 1,
            cohort=profile.cohort,
            profile=profile,
            customer_name=f"{given_name} {family_name}",
            given_name=given_name,
            family_name=family_name,
            # A small-business proprietor is still an individual party in this model; the BUSINESS
            # customer type is reserved for the entity, which this dataset does not separate out.
            customer_type=CustomerType.INDIVIDUAL,
            segment=segment,
            value_tier=value_tier,
            value_score=float(ctx.integer((score_low, score_high))),
            customer_since=customer_since,
            date_of_birth=date_of_birth,
            citizenship=None if profile.sparse_fields else ctx.weighted(vocab.CITIZENSHIPS),
            occupation=occupation,
            employment_status=employment_status,
            marital_status=marital_status,
            language=ctx.weighted(vocab.LANGUAGES),
            preferred_channel=(
                None if profile.sparse_fields else ctx.weighted(vocab.PREFERRED_CHANNELS)
            ),
            city=city,
            state=state,
            postal_code=postal,
            street_address=street,
            unit=unit,
            email=email,
            phone_number=phone,
            mobile_number=mobile,
            monthly_income=ctx.cents(profile.monthly_income_cents, round_to=100),
            employer_id=(
                None
                if profile.sparse_fields or employment_status in ("RETIRED", "UNEMPLOYED")
                else (
                    ADVERSARIAL_EMPLOYER_ID
                    if is_probe_carrier_index(index + 1)
                    else ctx.pick(employer_ids)
                )
            ),
            household_id=household.household_id if household is not None else None,
        )
        population.add_customer(plan)
        if household is not None:
            household.member_ids.append(customer_id)

    _assign_household_roles(population)
    return population


def _assign_household_roles(population: Population) -> None:
    """Assign household roles by age, and pick each household's primary customer.

    Runs after every customer exists because a role is a statement about a member's position
    relative
    to the others, which is not knowable while they are still being built one at a time.
    """
    for household in population.households:
        members = [population.by_id[member_id] for member_id in household.member_ids]
        # Oldest first. A missing date of birth (thin file) sorts youngest, since a sparse record is
        # far more likely to be a young customer than the head of a household.
        members.sort(
            key=lambda plan: (
                plan.date_of_birth or date(9999, 12, 31),
                plan.customer_id,
            )
        )

        head = members[0]
        household.primary_customer_id = head.customer_id
        household.roles[head.customer_id] = HouseholdRole.HEAD
        head.household_role = HouseholdRole.HEAD

        head_birth = head.date_of_birth
        for member in members[1:]:
            role = HouseholdRole.OTHER
            if head_birth is not None and member.date_of_birth is not None:
                gap_years = (member.date_of_birth - head_birth).days // 365
                if gap_years >= _GENERATION_GAP_YEARS:
                    role = HouseholdRole.CHILD
                elif gap_years <= _COUPLE_AGE_SPREAD:
                    role = (
                        HouseholdRole.SPOUSE
                        if head.marital_status in _MARRIED_STATUSES
                        else HouseholdRole.PARTNER
                    )
            elif member.date_of_birth is None:
                role = HouseholdRole.DEPENDENT
            household.roles[member.customer_id] = role
            member.household_role = role

        # Only one spouse or partner per household; later couples-by-age become OTHER.
        seen_partner = False
        for member in members[1:]:
            role = household.roles[member.customer_id]
            if role in (HouseholdRole.SPOUSE, HouseholdRole.PARTNER):
                if seen_partner:
                    household.roles[member.customer_id] = HouseholdRole.OTHER
                    member.household_role = HouseholdRole.OTHER
                seen_partner = True


def total_monthly_income(population: Population, household: HouseholdPlan) -> Cents:
    """Sum of member incomes, for the household rollups requirement 6.2 displays."""
    return sum(
        (population.by_id[member_id].monthly_income for member_id in household.member_ids),
        Cents(0),
    )
