"""Identity-provider selection (task 4.1).

One function turns :class:`~c360.core.config.Settings` into the bound
:class:`~c360.domain.ports.IdentityProvider`, so ``AUTH_PROVIDER`` is read in exactly one place and
the rest of the application depends on the port, never on which implementation backs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from c360.core.config import AuthProvider
from c360.security.local_provider import LocalIdentityProvider
from c360.security.oidc_provider import OidcIdentityProvider
from c360.security.tokens import TokenCodec

if TYPE_CHECKING:
    from c360.core.config import Settings
    from c360.domain.ports import IdentityProvider


def build_identity_provider(settings: Settings) -> IdentityProvider:
    """Build the identity provider ``AUTH_PROVIDER`` selects.

    The local provider is also the one that issues tokens, so the token route reaches for the
    concrete :class:`LocalIdentityProvider` via :func:`build_local_provider` rather than this
    port-typed factory. This function is what the authentication middleware binds, and it only ever
    needs :meth:`authenticate`.
    """
    if settings.auth_provider is AuthProvider.OIDC:
        return OidcIdentityProvider(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
        )
    return build_local_provider(settings)


def build_local_provider(settings: Settings) -> LocalIdentityProvider:
    """Build the local RS256 provider from settings.

    The signing key comes from ``JWT_PRIVATE_KEY_PATH`` when set and is ephemeral otherwise (see
    :class:`TokenCodec`). Kept separate from :func:`build_identity_provider` because the token
    endpoint needs the concrete provider's issuance methods, which the port does not expose.
    """
    configured = settings.jwt_private_key_path
    key_path = settings.resolve(configured) if configured else None
    codec = TokenCodec.from_key_path(key_path, issuer="c360-local")
    return LocalIdentityProvider(codec=codec, access_ttl_s=settings.jwt_access_ttl_s)
