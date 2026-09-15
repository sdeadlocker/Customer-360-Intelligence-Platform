"""RS256 token signing and verification (task 4.1).

The local provider issues asymmetric RS256 JWTs rather than symmetric HS256 ones on purpose: the
same key material shape (a private key to sign, a public key to verify) is what an enterprise IdP
uses, so the verification path the middleware exercises is the one that will still be exercised
after the OIDC swap. It also means a verifier never needs the signing secret.

Key material comes from ``JWT_PRIVATE_KEY_PATH`` when configured; when it is not — the default for
local development and for the test suite — an ephemeral keypair is generated in-process. An
ephemeral key is correct for those cases and only those: tokens signed with it die with the
process, which is exactly what a dev restart or a test run wants, and it means no key file has to be
checked in or generated before the app will start. A production deployment sets the path (and the
:class:`~c360.core.config.Settings` validator already asserts the file exists).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from c360.security.errors import AuthenticationError

if TYPE_CHECKING:
    from pathlib import Path

#: The only algorithm this module signs or accepts. Pinned so a token presenting ``alg: none`` or a
#: symmetric algorithm is rejected before verification — the classic JWT downgrade attack.
ALGORITHM: Final = "RS256"

#: Standard RSA key size for RS256. 2048 is the floor; larger only slows signing without buying
#: security relevant to short-lived access tokens.
_KEY_BITS: Final = 2048

#: Public exponent. 65537 is the universal, safe default.
_PUBLIC_EXPONENT: Final = 65537


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The claims the local provider puts in an access token, decoded and validated.

    ``sub`` is the user ID; ``role`` and the entitlement/knowledge claims are what the principal is
    rebuilt from. The custom claims live under a namespaced key so they cannot collide with a
    registered claim if this token is ever inspected by generic tooling.
    """

    subject: str
    role: str
    entitlement_kind: str
    entitlement_values: list[str]
    knowledge_levels: list[str]
    issued_at: int
    expires_at: int
    token_type: str


class TokenCodec:
    """Signs and verifies RS256 JWTs against one keypair.

    Holds the loaded key material. One instance is built per process from settings and shared; it
    carries no per-request state.
    """

    __slots__ = ("_issuer", "_private_pem", "_public_pem")

    def __init__(self, *, private_pem: bytes, public_pem: bytes, issuer: str) -> None:
        self._private_pem = private_pem
        self._public_pem = public_pem
        self._issuer = issuer

    @classmethod
    def from_key_path(cls, path: Path | None, *, issuer: str) -> TokenCodec:
        """Build a codec from a PEM private key file, or an ephemeral keypair when ``path`` is None.

        The private key file is expected to be an unencrypted PKCS#8 PEM. Its public half is
        derived rather than read from a second file, so there is one thing to configure and no way
        for the two halves to disagree.
        """
        if path is None:
            private_key = rsa.generate_private_key(
                public_exponent=_PUBLIC_EXPONENT, key_size=_KEY_BITS
            )
        else:
            private_key_obj = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(private_key_obj, rsa.RSAPrivateKey):
                raise AuthenticationError("configured JWT key is not an RSA private key")
            private_key = private_key_obj

        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return cls(private_pem=private_pem, public_pem=public_pem, issuer=issuer)

    def issue(
        self,
        *,
        subject: str,
        role: str,
        entitlement_kind: str,
        entitlement_values: list[str],
        knowledge_levels: list[str],
        ttl_seconds: int,
        token_type: str = "access",  # noqa: S107 - a claim label, not a credential
    ) -> str:
        """Sign and return a JWT carrying the principal-rebuilding claims."""
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": subject,
            "iss": self._issuer,
            "iat": now,
            "exp": now + ttl_seconds,
            "typ": token_type,
            "c360": {
                "role": role,
                "ent_kind": entitlement_kind,
                "ent_values": entitlement_values,
                "knowledge_levels": knowledge_levels,
            },
        }
        return jwt.encode(payload, self._private_pem, algorithm=ALGORITHM)

    def verify(self, token: str) -> TokenClaims:
        """Verify a token's signature and expiry and return its claims.

        Raises:
            AuthenticationError: signature invalid, token expired, issuer wrong, or a required
                claim missing. The distinction is collapsed on the way out.
        """
        try:
            payload = jwt.decode(
                token,
                self._public_pem,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.InvalidTokenError as exc:
            # Every PyJWT failure — expired, bad signature, wrong issuer, missing claim — becomes
            # the same opaque 401. The type name goes to the log; nothing else does.
            raise AuthenticationError(f"token rejected: {type(exc).__name__}") from exc

        custom = payload.get("c360")
        if not isinstance(custom, dict):
            raise AuthenticationError("token missing c360 claim block")
        try:
            return TokenClaims(
                subject=str(payload["sub"]),
                role=str(custom["role"]),
                entitlement_kind=str(custom["ent_kind"]),
                entitlement_values=[str(v) for v in custom.get("ent_values", [])],
                knowledge_levels=[str(v) for v in custom.get("knowledge_levels", [])],
                issued_at=int(payload["iat"]),
                expires_at=int(payload["exp"]),
                token_type=str(payload.get("typ", "access")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("token claims malformed") from exc

    @property
    def public_pem(self) -> bytes:
        """The PEM-encoded public key, for a verifier that is not this process."""
        return self._public_pem
