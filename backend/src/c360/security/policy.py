"""Field-masking policy - the role x field matrix from design §7.2 (task 4.4).

This is the second of the two authorization gates: given that a principal may see a customer at
all, *which fields are legible*. The matrix is transcribed here exactly once, as data, and resolved
into a :class:`FieldPolicy` per role. The serializer in :mod:`c360.security.serializer` reads a
policy; it never reads the matrix directly, so the matrix has a single source of truth and the
role x field test (task 4.5) asserts against the same table the serializer uses.

Every ``(role, group)`` pair is present. A missing pair would default-open — the safest default for
a masking table is to fail closed, but an *incomplete* table is worse than either default because
it is silent, so the module asserts completeness at import time rather than trusting the
transcription.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from c360.security.model import FieldGroup, MaskMode, Role

# ---------------------------------------------------------------- the design §7.2 matrix
# Rows are field groups, columns are roles. Transcribed verbatim from the design document; the
# ordering of both axes matches the table there so a reviewer can diff the two side by side.
_F = MaskMode.FULL
_P = MaskMode.PARTIAL
_H = MaskMode.HIDDEN
_B = MaskMode.BAND

# fmt: off
_MATRIX: Final[dict[FieldGroup, dict[Role, MaskMode]]] = {
    #                              RM  Wealth  Contact  Branch  Risk  Marketing
    FieldGroup.IDENTITY:          {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _F, Role.BRANCH: _F, Role.RISK: _F, Role.MARKETING: _P},  # noqa: E501
    FieldGroup.DATE_OF_BIRTH:     {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _P, Role.BRANCH: _P, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.STREET_ADDRESS:    {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _P, Role.BRANCH: _P, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.CONTACT:           {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _F, Role.BRANCH: _F, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.ACCOUNT_NUMBER:    {Role.RM: _P, Role.WEALTH_ADVISOR: _P, Role.CONTACT_CENTER: _P, Role.BRANCH: _P, Role.RISK: _P, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.CARD_NUMBER:       {Role.RM: _P, Role.WEALTH_ADVISOR: _P, Role.CONTACT_CENTER: _P, Role.BRANCH: _P, Role.RISK: _P, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.BALANCES:          {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _B, Role.BRANCH: _B, Role.RISK: _F, Role.MARKETING: _B},  # noqa: E501
    FieldGroup.INCOME_EXPENSE:    {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _H, Role.BRANCH: _B, Role.RISK: _F, Role.MARKETING: _B},  # noqa: E501
    FieldGroup.INVESTMENT_DETAIL: {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _H, Role.BRANCH: _H, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.CREDIT_SCORE:      {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _B, Role.BRANCH: _B, Role.RISK: _F, Role.MARKETING: _B},  # noqa: E501
    FieldGroup.RISK_SCORES:       {Role.RM: _B, Role.WEALTH_ADVISOR: _B, Role.CONTACT_CENTER: _B, Role.BRANCH: _B, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.DELINQUENCY:       {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _B, Role.BRANCH: _B, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.AML_PEP:           {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _H, Role.BRANCH: _H, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.VIN_PROPERTY:      {Role.RM: _P, Role.WEALTH_ADVISOR: _P, Role.CONTACT_CENTER: _H, Role.BRANCH: _H, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.MERCHANT_DETAIL:   {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _P, Role.BRANCH: _P, Role.RISK: _F, Role.MARKETING: _H},  # noqa: E501
    FieldGroup.OFFERS:            {Role.RM: _F, Role.WEALTH_ADVISOR: _F, Role.CONTACT_CENTER: _F, Role.BRANCH: _F, Role.RISK: _F, Role.MARKETING: _F},  # noqa: E501
}
# fmt: on


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """The masking decisions for one role, resolved from the matrix.

    A frozen mapping from :class:`FieldGroup` to :class:`MaskMode`. ``mode_for`` is the only access
    path the serializer needs; the whole mapping is exposed for the role x field test.
    """

    role: Role
    modes: dict[FieldGroup, MaskMode]

    def mode_for(self, group: FieldGroup) -> MaskMode:
        """Return the masking mode this role applies to ``group``.

        Defaults to :attr:`MaskMode.HIDDEN` if a group is somehow absent — failing closed. The
        import-time completeness check makes that branch unreachable in practice, but a masking
        lookup must never default to *visible*.
        """
        return self.modes.get(group, MaskMode.HIDDEN)

    def is_visible(self, group: FieldGroup) -> bool:
        """Whether ``group`` renders as a real, unmasked value for this role."""
        return self.mode_for(group) is MaskMode.FULL


def _assert_matrix_complete() -> None:
    """Fail at import if any ``(role, group)`` pair is missing from the matrix.

    A silent hole in a masking table is a leak waiting for the one role/field combination nobody
    wrote a test for. Checking here turns that into an import-time error the whole test suite trips
    over immediately.
    """
    missing: list[str] = []
    for group in FieldGroup:
        row = _MATRIX.get(group)
        if row is None:
            missing.append(f"field group {group} has no matrix row")
            continue
        for role in Role:
            if role not in row:
                missing.append(f"({role}, {group}) is missing")
    if missing:
        raise AssertionError("design §7.2 masking matrix is incomplete: " + "; ".join(missing))


_assert_matrix_complete()


def _build_policies() -> dict[Role, FieldPolicy]:
    policies: dict[Role, FieldPolicy] = {}
    for role in Role:
        modes = {group: _MATRIX[group][role] for group in FieldGroup}
        policies[role] = FieldPolicy(role=role, modes=modes)
    return policies


#: Resolved once at import; policies are immutable, so there is nothing to rebuild per request.
_POLICIES: Final[dict[Role, FieldPolicy]] = _build_policies()


def policy_for_role(role: Role) -> FieldPolicy:
    """Return the resolved :class:`FieldPolicy` for ``role``."""
    return _POLICIES[role]
