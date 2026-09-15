"""Local OAuth 2.0 identity provider (task 4.1).

Issues RS256 access and refresh tokens from a seeded user table with exactly one user per role, so
the six-login phase gate has six credentials to log in with. This is decision D6 in the design: a
local provider behind the :class:`~c360.domain.ports.IdentityProvider` port, swappable for the OIDC
adapter by ``AUTH_PROVIDER`` alone.

What it is and is not
---------------------

It is a faithful OAuth 2.0 *resource-server + token-endpoint* shape: password grant issues an
access token and a refresh token, the refresh grant exchanges a valid refresh token for a fresh
access token, and every access token is a signed, short-lived RS256 JWT the middleware verifies
without calling back here. It is *not* a credential store worth attacking: the seeded users have
fixed development passwords, because the whole point of the local provider is to make the platform
runnable and testable without an enterprise IdP. Production uses OIDC, where credentials never
touch this service at all.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Final

from c360.security.entitlement import scope_from_claim
from c360.security.errors import AuthenticationError
from c360.security.model import KnowledgeLevel, Principal, Role, knowledge_levels_for_role
from c360.security.policy import policy_for_role
from c360.security.tokens import TokenClaims, TokenCodec

#: The default token issuer for the local provider. Surfaces as the ``iss`` claim and is verified.
LOCAL_ISSUER: Final = "c360-local"

#: Refresh tokens outlive access tokens so a session survives access-token expiry without a fresh
#: login. A day is generous for a development provider and irrelevant to production (OIDC owns
#: refresh there); the idle-timeout in the middleware is the real session bound (requirement 12.10).
_REFRESH_TTL_S: Final = 86_400


@dataclass(frozen=True, slots=True)
class SeededUser:
    """One development user, one per role.

    ``entitlement_kind`` / ``entitlement_values`` are stored as the claim shape
    :func:`~c360.security.entitlement.scope_from_claim` consumes, so the seeded table and a real
    IdP's claims are the same data.
    """

    user_id: str
    password: str
    role: Role
    entitlement_kind: str
    entitlement_values: tuple[str, ...] = ()


#: One user per role (design §7.1). The RM is deliberately given a *restricted book* rather than
#: ALL, so the entitlement path (task 4.3) and the graph-redaction path (task 4.7) have a principal
#: that actually gets told "no" — an all-seeing default would let a leak hide. The book IDs use the
#: generator's stable customer-id format (``C-00001`` ...) so the RM has a real, non-empty book on
#: the seeded dataset; it is a *subset* (the first 80 of 100), so the RM still gets told "no" for
#: customers outside it, which is what exercises the entitlement and redaction paths.
_SEEDED_USERS: Final[tuple[SeededUser, ...]] = (
    SeededUser(
        user_id="rm.taylor",
        password="rm-dev-password",  # noqa: S106 - seeded local dev credential
        role=Role.RM,
        entitlement_kind="BOOK",
        entitlement_values=tuple(f"C-{n:05d}" for n in range(1, 81)),
    ),
    SeededUser(
        user_id="wealth.morgan",
        password="wealth-dev-password",  # noqa: S106 - seeded local dev credential
        role=Role.WEALTH_ADVISOR,
        entitlement_kind="SEGMENT",
        entitlement_values=("HNW", "UHNW", "AFFLUENT"),
    ),
    SeededUser(
        user_id="contact.jordan",
        password="contact-dev-password",  # noqa: S106 - seeded local dev credential
        role=Role.CONTACT_CENTER,
        entitlement_kind="ALL",
    ),
    SeededUser(
        user_id="branch.casey",
        password="branch-dev-password",  # noqa: S106 - seeded local dev credential
        role=Role.BRANCH,
        entitlement_kind="ALL",
    ),
    SeededUser(
        user_id="risk.riley",
        password="risk-dev-password",  # noqa: S106 - seeded local dev credential
        role=Role.RISK,
        entitlement_kind="ALL",
    ),
    SeededUser(
        user_id="marketing.avery",
        password="marketing-dev-password",  # noqa: S106 - seeded local dev credential
        role=Role.MARKETING,
        entitlement_kind="SEGMENT",
        entitlement_values=("MASS", "AFFLUENT"),
    ),
)

_USERS_BY_ID: Final[dict[str, SeededUser]] = {user.user_id: user for user in _SEEDED_USERS}


@dataclass(frozen=True, slots=True)
class TokenPair:
    """An issued access token and its refresh token."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 - the OAuth scheme name, not a credential
    expires_in: int = 0


class LocalIdentityProvider:
    """Seeded RS256 OAuth 2.0 provider satisfying :class:`~c360.domain.ports.IdentityProvider`."""

    __slots__ = ("_access_ttl_s", "_codec")

    def __init__(self, *, codec: TokenCodec, access_ttl_s: int) -> None:
        self._codec = codec
        self._access_ttl_s = access_ttl_s

    # ---------------------------------------------------------------- token endpoint
    def issue_for_password(self, user_id: str, password: str) -> TokenPair:
        """Password grant: authenticate a seeded user and issue a token pair.

        Raises:
            AuthenticationError: no such user, or the password does not match. The two are the same
                failure to the caller, so an attacker cannot enumerate valid usernames.
        """
        user = _USERS_BY_ID.get(user_id)
        # Compare even when the user is unknown, against a fixed dummy, so a wrong username and a
        # wrong password take the same time. `compare_digest` is constant-time within the compare.
        expected = user.password if user is not None else "\x00invalid"
        password_ok = hmac.compare_digest(
            hashlib.sha256(password.encode()).digest(),
            hashlib.sha256(expected.encode()).digest(),
        )
        if user is None or not password_ok:
            raise AuthenticationError("invalid username or password")
        return self._issue_pair(user)

    def refresh(self, refresh_token: str) -> TokenPair:
        """Refresh grant: exchange a valid refresh token for a fresh token pair.

        Raises:
            AuthenticationError: the refresh token is invalid, expired, not a refresh token, or its
                subject is no longer a seeded user.
        """
        claims = self._codec.verify(refresh_token)
        if claims.token_type != "refresh":  # noqa: S105 - claim label comparison
            raise AuthenticationError("not a refresh token")
        user = _USERS_BY_ID.get(claims.subject)
        if user is None:
            raise AuthenticationError("refresh subject is not a known user")
        return self._issue_pair(user)

    # ---------------------------------------------------------------- resource server
    def authenticate(self, access_token: str) -> Principal:
        """Validate an access token and build the principal (the :class:`IdentityProvider` port).

        Raises:
            AuthenticationError: the token is not a valid, unexpired access token, or its claims do
                not resolve to a known role/scope.
        """
        claims = self._codec.verify(access_token)
        if claims.token_type != "access":  # noqa: S105 - claim label comparison
            raise AuthenticationError("not an access token")
        return principal_from_claims(claims)

    # ---------------------------------------------------------------- internals
    def _issue_pair(self, user: SeededUser) -> TokenPair:
        levels = [str(level) for level in knowledge_levels_for_role(user.role)]
        values = list(user.entitlement_values)

        def _issue(ttl: int, token_type: str) -> str:
            return self._codec.issue(
                subject=user.user_id,
                role=str(user.role),
                entitlement_kind=user.entitlement_kind,
                entitlement_values=values,
                knowledge_levels=levels,
                ttl_seconds=ttl,
                token_type=token_type,
            )

        return TokenPair(
            access_token=_issue(self._access_ttl_s, "access"),
            refresh_token=_issue(_REFRESH_TTL_S, "refresh"),
            expires_in=self._access_ttl_s,
        )


def principal_from_claims(claims: TokenClaims) -> Principal:
    """Rebuild a :class:`Principal` from verified token claims.

    Shared by the local provider and the OIDC adapter (task 4.1): once a token's signature is
    trusted, both providers turn the same claim shape into the same principal, so the field policy
    and knowledge levels a role gets never depend on who issued the token.

    Raises:
        AuthenticationError: the role is not one of the six, or the entitlement claim is malformed.
    """
    try:
        role = Role(claims.role)
    except ValueError as exc:
        raise AuthenticationError("token carries an unknown role") from exc

    try:
        scope = scope_from_claim(claims.entitlement_kind, claims.entitlement_values)
    except ValueError as exc:
        raise AuthenticationError("token carries an invalid entitlement scope") from exc

    levels: frozenset[KnowledgeLevel] = _resolve_knowledge_levels(claims, role)
    return Principal(
        user_id=claims.subject,
        role=role,
        entitlement=scope,
        field_policy=policy_for_role(role),
        knowledge_levels=levels,
    )


def _resolve_knowledge_levels(claims: TokenClaims, role: Role) -> frozenset[KnowledgeLevel]:
    """Resolve the token's knowledge-level claim, intersected with what the role may ever hold.

    A token cannot grant a level the role's matrix entry does not allow — the claim is a
    convenience, the role table is the authority. If the claim is empty or unparseable, the role's
    full entitlement is used, so a token minted before a level existed still works.
    """
    allowed = knowledge_levels_for_role(role)
    claimed: set[KnowledgeLevel] = set()
    for value in claims.knowledge_levels:
        try:
            claimed.add(KnowledgeLevel(value))
        except ValueError:
            continue
    if not claimed:
        return allowed
    return frozenset(claimed & allowed)


def principal_for_user(user_id: str) -> Principal | None:
    """Build the *live* :class:`Principal` for a seeded user id, or ``None`` if unknown.

    The scheduled-report path (task 18.4) uses this to re-resolve a schedule owner's *current*
    entitlement at generation time rather than trusting the snapshot stored on the schedule: a user
    that no longer exists resolves to ``None`` and is skipped, and one whose role or book changed
    gets the new scope. Built from the same seeded table and role policy the token path uses, so a
    scheduled run is scoped and masked identically to that user's interactive requests.
    """
    user = _USERS_BY_ID.get(user_id)
    if user is None:
        return None
    return Principal(
        user_id=user.user_id,
        role=user.role,
        entitlement=scope_from_claim(user.entitlement_kind, list(user.entitlement_values)),
        field_policy=policy_for_role(user.role),
        knowledge_levels=knowledge_levels_for_role(user.role),
    )


def seeded_user_ids() -> tuple[str, ...]:
    """The seeded user IDs, one per role. Used by the token route's documentation and by tests."""
    return tuple(_USERS_BY_ID)


def seeded_users() -> tuple[SeededUser, ...]:
    """The seeded users. Test-support and documentation only."""
    return _SEEDED_USERS
