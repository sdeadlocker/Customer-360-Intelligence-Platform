"""Token endpoints for the local provider (task 4.1, 4.2).

``POST /auth/token`` is the password grant — the six seeded users log in here — and
``POST /auth/refresh`` is the refresh grant. Both are public routes (the authentication middleware
allowlists them, since a caller has no token yet). When ``AUTH_PROVIDER=oidc`` these endpoints have
no meaning — the enterprise IdP owns login — so they return 404, keeping the local-only surface
from masquerading as available in a deployment that does not use it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from c360.api.envelope import ApiError, Envelope, ErrorCode
from c360.core.config import AuthProvider, Settings
from c360.core.logging import get_logger
from c360.security.errors import AuthenticationError
from c360.security.local_provider import LocalIdentityProvider

router = APIRouter(tags=["auth"])

_logger = get_logger(__name__)


class TokenRequest(BaseModel):
    """Password-grant credentials."""

    model_config = ConfigDict(frozen=True)

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    """Refresh-grant token."""

    model_config = ConfigDict(frozen=True)

    refresh_token: str = Field(min_length=1)


class TokenPayload(BaseModel):
    """An issued token pair, in the OAuth 2.0 response shape."""

    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 - the scheme name, not a credential
    expires_in: int


def _local_provider(request: Request) -> LocalIdentityProvider:
    """Return the concrete local provider, or 404 when the deployment is OIDC."""
    settings: Settings = request.app.state.settings
    if settings.auth_provider is not AuthProvider.LOCAL:
        raise ApiError(
            ErrorCode.CUSTOMER_NOT_FOUND,
            "local token endpoint is not available under the configured identity provider",
        )
    provider = getattr(request.app.state, "local_identity_provider", None)
    if not isinstance(provider, LocalIdentityProvider):  # pragma: no cover - wiring guard
        raise ApiError(ErrorCode.UPSTREAM_UNAVAILABLE, "identity provider is not configured")
    return provider


@router.post(
    "/auth/token",
    response_model=Envelope[TokenPayload],
    summary="Password grant: exchange seeded credentials for a token pair",
    responses={401: {"description": "Invalid username or password"}},
)
async def issue_token(request: Request, body: TokenRequest) -> Envelope[TokenPayload]:
    """Authenticate a seeded user and issue an access/refresh token pair."""
    provider = _local_provider(request)
    try:
        pair = provider.issue_for_password(body.username, body.password)
    except AuthenticationError as exc:
        _logger.info("token issue rejected", extra={"reason": type(exc).__name__})
        raise ApiError(ErrorCode.UNAUTHENTICATED, "invalid username or password") from exc
    return Envelope.of(
        TokenPayload(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
        )
    )


@router.post(
    "/auth/refresh",
    response_model=Envelope[TokenPayload],
    summary="Refresh grant: exchange a refresh token for a fresh token pair",
    responses={401: {"description": "Invalid or expired refresh token"}},
)
async def refresh_token(request: Request, body: RefreshRequest) -> Envelope[TokenPayload]:
    """Exchange a valid refresh token for a fresh access/refresh pair."""
    provider = _local_provider(request)
    try:
        pair = provider.refresh(body.refresh_token)
    except AuthenticationError as exc:
        _logger.info("token refresh rejected", extra={"reason": type(exc).__name__})
        raise ApiError(ErrorCode.UNAUTHENTICATED, "invalid or expired refresh token") from exc
    return Envelope.of(
        TokenPayload(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
        )
    )
