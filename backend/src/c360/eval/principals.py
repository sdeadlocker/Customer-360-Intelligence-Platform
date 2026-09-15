"""Building principals for evaluation, one per role (tasks 11.2, 11.4).

The harness and the entitlement-safety scorer both need a :class:`Principal` for a role without
going
through the token endpoint -- there is no HTTP in an eval run. The security layer already exposes
the
pure pieces a principal is assembled from (:func:`policy_for_role`, :func:`scope_from_claim`,
:func:`knowledge_levels_for_role`), so this module just composes them, which keeps the eval
principals identical in shape to the ones the local provider mints from a token.

Two entitlement shapes matter for evaluation:

* **Full-visibility** principals (:func:`role_principals`) give every role ``ALL`` scope, so the
  harness can run every agent for every panel customer regardless of role. This is what the
  entitlement-safety scorer (task 11.4, dimension 6) needs: it runs *every* role over *every*
  customer and asserts no role's narrative contains a value that role's field policy forbids. The
  point is to catch a *masking* leak, not an entitlement (row-level) denial -- so the scope is
  deliberately open and the field policy is the thing under test.
* **Scoped** principals (:func:`scoped_principal`) reproduce a specific entitlement claim, used by
  the adversarial cross-customer and entitlement-probing categories where a *denial* is the
  expected, correct behaviour.
"""

from __future__ import annotations

from c360.security.entitlement import scope_from_claim
from c360.security.model import Principal, Role, knowledge_levels_for_role
from c360.security.policy import policy_for_role


def principal_for_role(
    role: Role, *, entitlement_kind: str = "ALL", values: tuple[str, ...] = ()
) -> Principal:
    """Construct a :class:`Principal` for ``role`` with the given entitlement claim.

    ``entitlement_kind`` / ``values`` are the same claim shape the token carries, so a principal
    built here is indistinguishable from one the local provider produces. The field policy and
    knowledge levels are always the role's authoritative matrix entries -- a claim cannot widen
    them.
    """
    return Principal(
        user_id=f"eval.{role.value.lower()}",
        role=role,
        entitlement=scope_from_claim(entitlement_kind, list(values)),
        field_policy=policy_for_role(role),
        knowledge_levels=knowledge_levels_for_role(role),
    )


def role_principals() -> dict[Role, Principal]:
    """One full-visibility (``ALL`` scope) principal per role (task 11.4).

    Row-level entitlement is opened to ALL so the entitlement-safety scorer exercises field-level
    masking across every role  x  customer pair without a row-level denial hiding a customer from a
    role. The field policy -- the thing that must not leak -- is the role's real one.
    """
    return {role: principal_for_role(role) for role in Role}


def scoped_principal(
    role: Role, *, entitlement_kind: str, values: tuple[str, ...] = ()
) -> Principal:
    """A principal with a specific, possibly-restrictive entitlement (task 11.5).

    Used by adversarial categories where a denial is the correct outcome: a ``BOOK`` scope limited
    to one customer, probed for another, must refuse.
    """
    return principal_for_role(role, entitlement_kind=entitlement_kind, values=values)


#: The role the dashboard harness runs its quality agents under (design panels are RM-facing). RM
#: sees the widest field set short of Risk, so a groundedness or coverage failure is not masked away
#: by a restrictive policy -- the harness measures the agent, not the mask.
HARNESS_ROLE: Role = Role.RM


__all__ = [
    "HARNESS_ROLE",
    "principal_for_role",
    "role_principals",
    "scoped_principal",
]
