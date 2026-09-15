"""Authentication middleware, session enforcement and the principal dependency (task 4.2).

Requirement 12.1: every API route requires a valid OAuth 2.0 access token and an unauthenticated
request is rejected with 401. Requirement 12.10: an idle session past the configured window is
terminated. Both are enforced here, once, at the edge — a route handler receives a request only
after a principal has been validated and bound to the context, so no handler can forget to check.

Why middleware plus a dependency, not one or the other
------------------------------------------------------

The middleware does the enforcing: it runs on every request, extracts and validates the bearer
token, applies the idle-timeout rule, and binds the :class:`Principal` to the request context so
services and the audit writer can read "who is asking" without it being threaded through every
call. The :func:`current_principal` dependency does the *injecting*: a handler that wants the
principal as a typed argument declares it and gets the same object the middleware bound. Splitting
it this way means the security decision is unavoidable (middleware) while the ergonomics are
opt-in (dependency).

A small set of routes are public by construction — liveness, readiness, the token endpoint itself,
and the OpenAPI docs. They are matched by exact path prefix rather than by decorator so the
allowlist is visible in one place and a new protected route is protected by default.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

import jwt
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from c360.api.envelope import ApiError, ErrorCode, ErrorEnvelope
from c360.core.context import get_principal, reset_principal, set_principal
from c360.core.logging import get_logger
from c360.core.telemetry import SpanAttr
from c360.security.errors import AuthenticationError
from c360.security.tokens import TokenCodec

if TYPE_CHECKING:
    from c360.domain.ports import IdentityProvider
    from c360.security.model import Principal
    from c360.security.session import IdleSessionTracker

_logger = get_logger(__name__)

#: Routes reachable without a token. Prefixes, not exact matches, so ``/docs`` covers the assets the
#: docs page pulls. Everything not under one of these requires authentication.
_PUBLIC_PREFIXES: Final[tuple[str, ...]] = (
    "/health",
    "/ready",
    "/auth/token",
    "/auth/refresh",
    "/docs",
    "/redoc",
    "/openapi.json",
)

#: The one accepted authorization scheme.
_BEARER_PREFIX: Final = "Bearer "


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Validate the bearer token, enforce idle timeout, bind the principal.

    Holds the identity provider and the idle-session tracker, both built once at app startup. Being
    middleware, it runs inside the correlation-ID scope (that middleware is added later and so runs
    first), which means a 401 it emits already carries a correlation ID.
    """

    def __init__(
        self,
        app: object,
        *,
        identity_provider: IdentityProvider,
        session_tracker: IdleSessionTracker,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._identity_provider = identity_provider
        self._session_tracker = session_tracker

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)

        token = _bearer_token(request)
        if token is None:
            return _unauthenticated("missing or malformed Authorization header")

        try:
            principal = self._identity_provider.authenticate(token)
        except AuthenticationError as exc:
            # The reason is logged (redacted type name only) but never returned to the caller.
            _logger.info("authentication rejected", extra={"reason": type(exc).__name__})
            return _unauthenticated("the access token is missing, invalid or expired")

        if self._is_session_idle(token, principal):
            _logger.info("session idle-timeout", extra={"role": str(principal.role)})
            return _unauthenticated("the session has expired due to inactivity")

        self._session_tracker.touch(principal.user_id)
        self._annotate_span(principal)

        context_token = set_principal(principal)
        try:
            response = await call_next(request)
        finally:
            reset_principal(context_token)
        return response

    def _is_session_idle(self, token: str, principal: Principal) -> bool:
        """Apply the idle-timeout rule, reading the token's issue time as the activity floor."""
        issued_at = _token_issued_at(token)
        return self._session_tracker.is_expired(
            principal.user_id, token_issued_at=issued_at, now=time.time()
        )

    @staticmethod
    def _annotate_span(principal: Principal) -> None:
        """Put the role (not the user ID) on the span. Role is allowlisted; a user ID is not."""
        span = trace.get_current_span()
        if span.get_span_context().is_valid:
            span.set_attribute(SpanAttr.ROLE, str(principal.role))


def _is_public(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if header is None or not header.startswith(_BEARER_PREFIX):
        return None
    token = header[len(_BEARER_PREFIX) :].strip()
    return token or None


def _token_issued_at(token: str) -> int:
    """Read ``iat`` from a token without verifying it — the signature was already checked.

    The middleware has already validated the token via the provider before this is called, so
    decoding the claims again only to read one number would be wasteful. This reads the unverified
    ``iat`` purely as the idle-timeout activity floor; a forged ``iat`` can only *shorten* a
    session (a future value fails the window), never extend one, so trusting it here is safe.
    """
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        return int(payload.get("iat", 0))
    except (jwt.InvalidTokenError, TypeError, ValueError):
        return 0


def _unauthenticated(message: str) -> JSONResponse:
    """Build the coded 401 envelope directly, since a middleware runs outside the handler stack."""
    return JSONResponse(
        status_code=401,
        content=ErrorEnvelope.of(ErrorCode.UNAUTHENTICATED, message).model_dump(mode="json"),
    )


def current_principal(request: Request) -> Principal:
    """FastAPI dependency returning the principal the middleware bound.

    Reads from the request scope rather than the contextvar so it composes with FastAPI's
    dependency cache and testing overrides; the middleware binds both. Raises if called on a public
    route (there is no principal there) - which is a routing mistake, not a runtime condition.
    """
    principal = get_principal()
    if principal is None:  # pragma: no cover - defends a routing mistake
        raise ApiError(ErrorCode.UNAUTHENTICATED, "authentication required")
    return principal


__all__ = [
    "AuthenticationMiddleware",
    "TokenCodec",
    "current_principal",
]
