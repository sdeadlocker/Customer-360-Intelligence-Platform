"""Households, relationships, joint parties and beneficiaries (task 2.5).

Assets are generated in :mod:`c360.generator.products`, because a mortgage needs its collateral to
exist first. What is left here is the graph: who is related to whom, who else is on an account, and
who
inherits it.

Depth is a requirement, not a flourish
--------------------------------------

Task 2.5 asks for "depth sufficient to exercise 3-hop traversal", and requirement 6.7 puts a p95
budget
on that traversal. A dataset of self-contained households cannot test either: every path terminates
at
the household boundary after one hop, so a 3-hop query is indistinguishable from a 1-hop query and
the
recursive CTE in Phase 5.3 is never exercised at depth.

:func:`_link_across_households` is what creates the depth. It chains household heads with
``BUSINESS_PARTNER``, ``REFERRED_BY`` and ``ADVISOR`` edges, so a path like *spouse → head →
business partner → their spouse* is three hops and crosses two households. Combined with the
isolated
cohort — which gets no edges at all — the seeded graph contains both the deep case and the empty
case.

Inferred versus system-of-record
--------------------------------

Requirement 6.6 requires the two to render differently, because *a spouse link guessed from a shared
address is not the same fact as a joint account holder*. The split here is deliberate rather than
random:

* a ``SPOUSE`` link, where the household says the members are married, is ``SYSTEM_OF_RECORD``;
* a ``PARTNER`` link, which the bank has only inferred from a shared address, is ``INFERRED`` and
  carries a confidence and an ``inference_basis`` naming the shared address hash;
* ``SIBLING`` between two children of one head is ``INFERRED``, because the bank never recorded it.

Migration ``0003`` enforces the pairing with ``CHECK (is_inferred = 0 OR (confidence IS NOT NULL AND
inference_basis IS NOT NULL))``, so an inferred edge without its justification cannot be written.
"""

from __future__ import annotations

from typing import Final

from c360.domain.enums import (
    AccountStatus,
    AccountType,
    HouseholdRole,
    PartyRole,
    RelationshipType,
)
from c360.generator import vocab
from c360.generator.context import (
    SOURCE_CORE_BANKING,
    SOURCE_CRM,
    SOURCE_MDM,
    GeneratorContext,
)
from c360.generator.plan import CustomerPlan, HouseholdPlan, Population
from c360.generator.tables import (
    ACCOUNT_PARTY,
    BENEFICIARY,
    CUSTOMER_RELATIONSHIP,
    HOUSEHOLD_MEMBER,
    Dataset,
)

#: Confidence bounds, in percent, for an inferred relationship.
_INFERRED_CONFIDENCE_PERCENT: Final = (58, 94)

#: Percent of eligible deposit and investment accounts held jointly with a household partner.
_JOINT_ACCOUNT_PERCENT: Final = 45

#: Percent of accounts that name at least one beneficiary.
_BENEFICIARY_PERCENT: Final = 38

#: Percent of beneficiary records that were inferred rather than declared.
_INFERRED_BENEFICIARY_PERCENT: Final = 22

#: Share splits, in basis points, by beneficiary count. Each row sums to 10,000 so an account's
#: beneficiary shares reconcile, which is the property ``share_bps`` exists to make checkable.
_BENEFICIARY_SHARES: Final[dict[int, tuple[int, ...]]] = {
    1: (10_000,),
    2: (6_000, 4_000),
    3: (5_000, 3_000, 2_000),
}

#: Percent of household heads linked to the next head in the chain, which is what creates the
#: cross-household depth a 3-hop traversal needs.
_CROSS_HOUSEHOLD_LINK_PERCENT: Final = 55

#: A relationship needs two distinct parties, so cross-household linking needs at least two heads.
_MIN_HEADS_FOR_A_LINK: Final = 2

#: Relationship types used for cross-household links, with weights.
_CROSS_HOUSEHOLD_TYPES: Final[tuple[tuple[RelationshipType, int], ...]] = (
    (RelationshipType.BUSINESS_PARTNER, 40),
    (RelationshipType.REFERRED_BY, 35),
    (RelationshipType.ADVISOR, 25),
)

#: Account types that can be held jointly.
_JOINTABLE: Final = (AccountType.DEPOSIT, AccountType.INVESTMENT)

#: Account types that can name a beneficiary.
_BENEFICIARY_ELIGIBLE: Final = (AccountType.DEPOSIT, AccountType.INVESTMENT)


class _RelationshipWriter:
    """Emits ``customer_relationship`` rows, refusing duplicates and self-links.

    The table carries ``UNIQUE (from_customer_id, to_customer_id, relationship_type)`` and
    ``CHECK (from_customer_id <> to_customer_id)``. Both are easy to violate here — a sibling pair
    reached from two different heads, a household of one where head and partner resolve to the same
    person — so the guard lives at the single point rows are written rather than at each call site.

    The ``_seen`` set is only ever tested for membership, never iterated, so it cannot leak
    process-dependent ordering into the output (see :mod:`c360.generator.context`).
    """

    __slots__ = ("_ctx", "_dataset", "_seen", "_sequence")

    def __init__(self, ctx: GeneratorContext, dataset: Dataset) -> None:
        self._ctx = ctx
        self._dataset = dataset
        self._seen: set[tuple[str, str, str]] = set()
        self._sequence = 0

    def write(
        self,
        *,
        from_id: str,
        to_id: str,
        relationship_type: RelationshipType,
        inferred: bool,
        basis: str | None = None,
        source_system: str = SOURCE_CORE_BANKING,
    ) -> bool:
        """Emit one relationship. Returns whether it was written."""
        if from_id == to_id:
            return False
        key = (from_id, to_id, relationship_type.value)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._sequence += 1

        confidence = self._ctx.integer(_INFERRED_CONFIDENCE_PERCENT) / 100 if inferred else None
        self._dataset.add(
            CUSTOMER_RELATIONSHIP,
            (
                self._ctx.entity_id("R", self._sequence),
                from_id,
                to_id,
                relationship_type.value,
                1 if inferred else 0,
                confidence,
                basis,
                source_system,
                self._ctx.as_of_iso,
            ),
        )
        return True


def _emit_household_members(
    ctx: GeneratorContext, population: Population, dataset: Dataset
) -> None:
    for household in population.households:
        for member_id in household.member_ids:
            plan = population.by_id[member_id]
            role = household.roles.get(member_id, HouseholdRole.OTHER)
            joined = max(plan.customer_since, ctx.history_start)
            dataset.add(
                HOUSEHOLD_MEMBER,
                (
                    household.household_id,
                    member_id,
                    role.value,
                    min(joined, ctx.as_of).isoformat(),
                    ctx.as_of_iso,
                ),
            )


def _link_within_household(
    ctx: GeneratorContext,
    population: Population,
    household: HouseholdPlan,
    writer: _RelationshipWriter,
) -> None:
    """Create the family edges implied by the household roles."""
    head_id = household.primary_customer_id
    if head_id is None:
        return
    head = population.by_id[head_id]
    if not head.profile.has_relationships:
        return

    children: list[str] = []
    for member_id in household.member_ids:
        if member_id == head_id:
            continue
        member = population.by_id[member_id]
        if not member.profile.has_relationships:
            continue
        role = household.roles.get(member_id, HouseholdRole.OTHER)

        if role is HouseholdRole.SPOUSE:
            # Marriage is recorded, not guessed.
            writer.write(
                from_id=head_id,
                to_id=member_id,
                relationship_type=RelationshipType.SPOUSE,
                inferred=False,
                source_system=SOURCE_MDM,
            )
            writer.write(
                from_id=member_id,
                to_id=head_id,
                relationship_type=RelationshipType.SPOUSE,
                inferred=False,
                source_system=SOURCE_MDM,
            )
        elif role is HouseholdRole.PARTNER:
            # Guessed from the shared address, so it is labelled and scored.
            basis = f"SHARED_ADDRESS_HASH:{household.address_hash}"
            writer.write(
                from_id=head_id,
                to_id=member_id,
                relationship_type=RelationshipType.PARTNER,
                inferred=True,
                basis=basis,
                source_system=SOURCE_MDM,
            )
        elif role in (HouseholdRole.CHILD, HouseholdRole.DEPENDENT):
            children.append(member_id)
            writer.write(
                from_id=head_id,
                to_id=member_id,
                relationship_type=RelationshipType.PARENT,
                inferred=False,
                source_system=SOURCE_MDM,
            )
            writer.write(
                from_id=member_id,
                to_id=head_id,
                relationship_type=RelationshipType.CHILD,
                inferred=False,
                source_system=SOURCE_MDM,
            )

    # Siblings are never recorded by a bank; they are inferred from sharing a parent.
    for index, first in enumerate(children):
        for second in children[index + 1 :]:
            basis = f"SHARED_PARENT:{head_id}"
            writer.write(
                from_id=first,
                to_id=second,
                relationship_type=RelationshipType.SIBLING,
                inferred=True,
                basis=basis,
                source_system=SOURCE_MDM,
            )


def _link_across_households(
    ctx: GeneratorContext,
    population: Population,
    writer: _RelationshipWriter,
) -> None:
    """Chain household heads together so paths longer than one hop exist.

    Consecutive heads are linked rather than random pairs, because a random graph on 35 households
    produces mostly isolated pairs and a few dense clusters; a chain with random skips guarantees
    long
    paths exist while still leaving branching.
    """
    heads = [
        household.primary_customer_id
        for household in population.households
        if household.primary_customer_id is not None
        and population.by_id[household.primary_customer_id].profile.has_relationships
    ]
    if len(heads) < _MIN_HEADS_FOR_A_LINK:
        return

    for index in range(len(heads) - 1):
        if not ctx.chance(_CROSS_HOUSEHOLD_LINK_PERCENT):
            continue
        relationship_type = ctx.weighted(_CROSS_HOUSEHOLD_TYPES)
        inferred = relationship_type is RelationshipType.BUSINESS_PARTNER and ctx.chance(40)
        writer.write(
            from_id=heads[index],
            to_id=heads[index + 1],
            relationship_type=relationship_type,
            inferred=inferred,
            basis="SHARED_EMPLOYER" if inferred else None,
            source_system=SOURCE_CRM,
        )

    # A few long-range edges, so the graph is not a bare path and centrality has something to rank.
    extra = max(1, len(heads) // 6)
    for _ in range(extra):
        first, second = ctx.sample(heads, _MIN_HEADS_FOR_A_LINK)
        writer.write(
            from_id=first,
            to_id=second,
            relationship_type=RelationshipType.REFERRED_BY,
            inferred=False,
            source_system=SOURCE_CRM,
        )


def _emit_account_parties(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    """Record the owner of every account, and a joint holder on some of them.

    Every account gets a ``PRIMARY`` party. That is redundant with ``account.customer_id`` and it is
    written anyway, because requirement 6.1 renders account parties as a relationship and a joint
    account whose primary holder is absent from the party list reads as a missing row rather than as
    an
    implied one.
    """
    for plan in population.customers:
        partner_id = _household_partner(population, plan)
        for account in plan.accounts:
            # Decided before either row is written, so the primary's share is correct the first time
            # rather than being patched afterwards.
            is_joint = (
                partner_id is not None
                and account.account_type in _JOINTABLE
                and account.status is not AccountStatus.CLOSED
                and ctx.chance(_JOINT_ACCOUNT_PERCENT)
            )
            # Shares on one account sum to 10,000 bps either way.
            primary_share = 5_000 if is_joint else 10_000
            opened = account.open_date.isoformat()

            dataset.add(
                ACCOUNT_PARTY,
                (
                    account.account_id,
                    plan.customer_id,
                    PartyRole.PRIMARY.value,
                    primary_share,
                    opened,
                    ctx.as_of_iso,
                ),
            )
            if is_joint and partner_id is not None:
                dataset.add(
                    ACCOUNT_PARTY,
                    (
                        account.account_id,
                        partner_id,
                        PartyRole.JOINT.value,
                        5_000,
                        opened,
                        ctx.as_of_iso,
                    ),
                )


def _household_partner(population: Population, plan: CustomerPlan) -> str | None:
    """The spouse or partner in ``plan``'s household, if there is one."""
    if plan.household_id is None:
        return None
    for household in population.households:
        if household.household_id != plan.household_id:
            continue
        for member_id in household.member_ids:
            if member_id == plan.customer_id:
                continue
            role = household.roles.get(member_id)
            if role in (HouseholdRole.SPOUSE, HouseholdRole.PARTNER):
                return member_id
        # A child can be a joint party on a custodial account, but not a spouse.
        return None
    return None


def _emit_beneficiaries(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    """Name beneficiaries on some accounts.

    ``beneficiary_customer_id`` is left NULL for a named person who is not a customer of the bank,
    which is the common case in reality and the reason the column is nullable. Both shapes are
    generated so the relationship graph has to handle a node that is a name rather than a customer.
    """
    sequence = 0
    for plan in population.customers:
        household_members = _household_others(population, plan)
        for account in plan.accounts:
            if account.account_type not in _BENEFICIARY_ELIGIBLE:
                continue
            if account.status is AccountStatus.CLOSED or not ctx.chance(_BENEFICIARY_PERCENT):
                continue

            count = ctx.weighted(((1, 62), (2, 28), (3, 10)))
            shares = _BENEFICIARY_SHARES[count]
            for share in shares:
                sequence += 1
                inferred = ctx.chance(_INFERRED_BENEFICIARY_PERCENT)
                use_customer = bool(household_members) and ctx.chance(65)
                if use_customer:
                    beneficiary_id = ctx.pick(household_members)
                    name = population.by_id[beneficiary_id].customer_name
                else:
                    beneficiary_id = None
                    name = f"{ctx.pick(vocab.GIVEN_NAMES)} {ctx.pick(vocab.FAMILY_NAMES)}"

                dataset.add(
                    BENEFICIARY,
                    (
                        ctx.entity_id("B", sequence),
                        account.account_id,
                        beneficiary_id,
                        name,
                        ctx.pick(("SPOUSE", "CHILD", "SIBLING", "PARENT", "OTHER")),
                        share,
                        1 if inferred else 0,
                        ctx.integer(_INFERRED_CONFIDENCE_PERCENT) / 100 if inferred else None,
                        ctx.as_of_iso,
                    ),
                )


def _household_others(population: Population, plan: CustomerPlan) -> list[str]:
    """Other members of ``plan``'s household, in a stable order."""
    if plan.household_id is None:
        return []
    for household in population.households:
        if household.household_id == plan.household_id:
            return [
                member_id for member_id in household.member_ids if member_id != plan.customer_id
            ]
    return []


def generate_network(ctx: GeneratorContext, population: Population, dataset: Dataset) -> None:
    """Emit household membership, relationships, account parties and beneficiaries."""
    _emit_household_members(ctx, population, dataset)

    writer = _RelationshipWriter(ctx, dataset)
    for household in population.households:
        _link_within_household(ctx, population, household, writer)
    _link_across_households(ctx, population, writer)

    _emit_account_parties(ctx, population, dataset)
    _emit_beneficiaries(ctx, population, dataset)
