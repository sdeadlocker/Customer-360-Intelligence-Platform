"""OIDC identity provider (task 4.1).

The production authentication path (requirement 12.2): validate an enterprise IdP's access tokens
against the issuer's published JWKS and map its claims onto the same :class:`Principal` the local
provider builds. Selected by ``AUTH_PROVIDER=oidc``.

This adapter validates and maps; it never issues. An enterprise IdP owns issuance, refresh and the
login UI, so the local provider's token endpoint has no analogue here — which is why
:class:`~c360.domain.ports.IdentityProvider` declares only :meth:`authenticate`.

Signing keys are fetched from the issuer's JWKS endpoint and cached by :class:`jwt.PyJWKClient`, so
a key rotation is picked up without a redeploy and steady-state verification costs no network round
trip. The claim mapping is deliberately conservative: an unknown role or a missing entitlement
claim is a rejected token, not a default-open principal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import jwt
from jwt import PyJWKClient

from c360.security.errors import AuthenticationError
from c360.security.local_provider import principal_from_claims
from c360.security.tokens import TokenClaims

if TYPE_CHECKING:
    from c360.security.model import Principal

#: Algorithms accepted from the IdP. RS256 only, for the same downgrade-resistance reason the local
#: provider pins it: accepting a symmetric or ``none`` algorithm from an external token is the
#: canonical JWT vulnerability.
_ACCEPTED_ALGORITHMS: Final = ["RS256"]

#: Where the role and entitlement live in an IdP token. A real deployment configures its IdP to emit
#: these; the names are namespaced so they do not collide with the IdP's own claims.
_ROLE_CLAIM: Final = "c360_role"
_ENT_KIND_CLAIM: Final = "c360_entitlement_kind"
_ENT_VALUES_CLAIM: Final = "c360_entitlement_values"
_KNOWLEDGE_CLAIM: Final = "c360_knowledge_levels"


class OidcIdentityProvider:
    """Validates enterprise IdP tokens via JWKS. Satisfies :class:`IdentityProvider`."""

    __slots__ = ("_audience", "_issuer", "_jwks_client")

    def __init__(
        self, *, issuer: str, client_id: str, jwks_client: PyJWKClient | None = None
    ) -> None:
        self._issuer = issuer
        self._audience = client_id
        # The JWKS URI follows the OIDC discovery convention. A caller may inject a client (a test
        # double, or one pointed at a discovered URI) rather than have one built from the issuer.
        self._jwks_client = jwks_client or PyJWKClient(
            f"{issuer.rstrip('/')}/.well-known/jwks.json"
        )

    def authenticate(self, access_token: str) -> Principal:
        """Validate against JWKS and map claims onto a principal.

        Raises:
            AuthenticationError: signature invalid against every published key, token expired,
                issuer or audience wrong, or the c360 claims missing/unmappable.
        """
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(access_token)
            payload: dict[str, Any] = jwt.decode(
                access_token,
                signing_key.key,
                algorithms=_ACCEPTED_ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError(f"oidc token rejected: {type(exc).__name__}") from exc
        except jwt.PyJWKClientError as exc:
            raise AuthenticationError("oidc signing key unavailable") from exc

        claims = self._map_claims(payload)
        return principal_from_claims(claims)

    def _map_claims(self, payload: dict[str, Any]) -> TokenClaims:
        """Project an IdP payload onto the internal :class:`TokenClaims` shape.

        Raises:
            AuthenticationError: a required c360 claim is absent.
        """
        role = payload.get(_ROLE_CLAIM)
        ent_kind = payload.get(_ENT_KIND_CLAIM)
        if not isinstance(role, str) or not isinstance(ent_kind, str):
            raise AuthenticationError("oidc token missing role or entitlement claim")
        return TokenClaims(
            subject=str(payload["sub"]),
            role=role,
            entitlement_kind=ent_kind,
            entitlement_values=[str(v) for v in payload.get(_ENT_VALUES_CLAIM, [])],
            knowledge_levels=[str(v) for v in payload.get(_KNOWLEDGE_CLAIM, [])],
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            token_type="access",  # noqa: S106 - claim label, not a credential
        )
