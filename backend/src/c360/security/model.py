"""Security value types shared across the platform (task 4.1).

Design §7.1 makes every :class:`IdentityProvider` — local or OIDC — produce the same
:class:`Principal`, so the rest of the platform never learns which provider issued a token. This
module defines that shape and the closed sets it is built from: the six :class:`Role` values, the
four masking :class:`MaskMode` values, the :class:`FieldGroup` rows of the design §7.2 matrix, and
the :class:`KnowledgeLevel` set the RAG pre-filter (§17.8) reads.

The masking matrix itself lives in :mod:`c360.security.policy`; the entitlement scope variants live
in :mod:`c360.security.entitlement`. They are separate modules because a change to "who may see
this customer" (row-level) and a change to "which fields are legible" (field-level) are independent
edits — design §7.2 calls them two independent gates, and keeping them in one file would blur that.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from c360.security.entitlement import EntitlementScope
    from c360.security.policy import FieldPolicy


class Role(StrEnum):
    """The six seeded roles from design §7.1.

    Members mirror the ``role`` claim the local provider issues and the values the OIDC claim
    mapping produces, so a role name is usable directly as a JWT claim and as an audit column with
    no translation.
    """

    RM = "RM"
    WEALTH_ADVISOR = "WEALTH_ADVISOR"
    CONTACT_CENTER = "CONTACT_CENTER"
    BRANCH = "BRANCH"
    RISK = "RISK"
    MARKETING = "MARKETING"


class MaskMode(StrEnum):
    """How a field group is rendered for a role (design §7.2).

    ``FULL`` = visible · ``PARTIAL`` = last-4 / year-only / city-only · ``HIDDEN`` = omitted with a
    ``masked_fields`` entry · ``BAND`` = a coarse band instead of a numeric value.
    """

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    HIDDEN = "HIDDEN"
    BAND = "BAND"


class FieldGroup(StrEnum):
    """The field-group rows of the design §7.2 masking matrix.

    Masking policy is expressed per *group* rather than per *field* because the design matrix is:
    "Balances, net worth" is one decision, not one decision per balance column. The serializer
    (task 4.4) maps each concrete model field onto one of these groups and looks the mode up here.
    """

    IDENTITY = "IDENTITY"  # name, segment, value tier
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    STREET_ADDRESS = "STREET_ADDRESS"
    CONTACT = "CONTACT"  # email / phone / mobile
    ACCOUNT_NUMBER = "ACCOUNT_NUMBER"
    CARD_NUMBER = "CARD_NUMBER"
    BALANCES = "BALANCES"  # balances, net worth
    INCOME_EXPENSE = "INCOME_EXPENSE"  # monthly income / expense
    INVESTMENT_DETAIL = "INVESTMENT_DETAIL"
    CREDIT_SCORE = "CREDIT_SCORE"  # FICO / behavior score
    RISK_SCORES = "RISK_SCORES"  # risk / fraud / PID / SID
    DELINQUENCY = "DELINQUENCY"  # delinquency, DPD, charge-off
    AML_PEP = "AML_PEP"  # AML / PEP flags
    VIN_PROPERTY = "VIN_PROPERTY"  # VIN, property address
    MERCHANT_DETAIL = "MERCHANT_DETAIL"  # transaction merchant detail
    OFFERS = "OFFERS"  # offers, propensity


class KnowledgeLevel(StrEnum):
    """Access level carried by a knowledge document (design §7.2, §17.8).

    The RAG layer (Phase 7) pre-filters retrieval on ``access_level IN principal.knowledge_levels``
    in SQL, before ranking, so a document above a role's entitlement is excluded rather than merely
    ranked low — and its existence is never revealed (requirement 17.8).
    """

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RISK_ONLY = "RISK_ONLY"
    COMPLIANCE_ONLY = "COMPLIANCE_ONLY"


#: Knowledge levels each role may retrieve (design §7.2 second matrix). Risk sees everything;
#: Marketing sees only public material. Everyone else sees public and internal.
KNOWLEDGE_LEVELS_BY_ROLE: Mapping[Role, frozenset[KnowledgeLevel]] = {
    Role.RM: frozenset({KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL}),
    Role.WEALTH_ADVISOR: frozenset({KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL}),
    Role.CONTACT_CENTER: frozenset({KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL}),
    Role.BRANCH: frozenset({KnowledgeLevel.PUBLIC, KnowledgeLevel.INTERNAL}),
    Role.RISK: frozenset(
        {
            KnowledgeLevel.PUBLIC,
            KnowledgeLevel.INTERNAL,
            KnowledgeLevel.RISK_ONLY,
            KnowledgeLevel.COMPLIANCE_ONLY,
        }
    ),
    Role.MARKETING: frozenset({KnowledgeLevel.PUBLIC}),
}


def knowledge_levels_for_role(role: Role) -> frozenset[KnowledgeLevel]:
    """Return the knowledge levels ``role`` is entitled to retrieve."""
    return KNOWLEDGE_LEVELS_BY_ROLE[role]


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, identical whatever provider issued the token (design §7.1).

    ``frozen`` because a principal is derived once per request from a validated token and read
    everywhere after; a mutable principal would be an authorization decision a later layer could
    quietly rewrite. It is a dataclass rather than a Pydantic model because it never crosses the
    serialization boundary — it is never a response body — and carrying non-JSON members
    (``EntitlementScope``, ``FieldPolicy``) is natural here.
    """

    user_id: str
    role: Role
    entitlement: EntitlementScope
    field_policy: FieldPolicy
    knowledge_levels: frozenset[KnowledgeLevel]
