"""Exception handlers producing the uniform error envelope from design §6.3.

Two rules drive every handler here:

* the response body is always ``{"error": {code, message, correlation_id}}``, so a client never has
  to branch on where a failure came from;
* messages never include field values. A validation error reports *which* field failed, not what
  was in it — an error body is a log line in disguise, and requirement 18.8 applies to it too.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from c360.api.envelope import ApiError, ErrorCode, ErrorEnvelope
from c360.core.logging import get_logger
from c360.security.errors import (
    AuditUnavailableError,
    AuthenticationError,
    EntitlementError,
)

_logger = get_logger(__name__)

#: Status codes FastAPI raises internally, mapped onto the documented code set.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHENTICATED,
    status.HTTP_403_FORBIDDEN: ErrorCode.ENTITLEMENT_DENIED,
    status.HTTP_404_NOT_FOUND: ErrorCode.CUSTOMER_NOT_FOUND,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.VALIDATION_ERROR,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
    status.HTTP_503_SERVICE_UNAVAILABLE: ErrorCode.UPSTREAM_UNAVAILABLE,
    status.HTTP_504_GATEWAY_TIMEOUT: ErrorCode.AGENT_TIMEOUT,
}


def _envelope(code: ErrorCode, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorEnvelope.of(code, message).model_dump(mode="json"),
    )


async def handle_api_error(_: Request, exc: Exception) -> JSONResponse:
    """Application-raised, already-coded failure."""
    assert isinstance(exc, ApiError)  # noqa: S101 - handler is registered for this type only
    _logger.info(
        "request rejected",
        extra={"error_code": str(exc.code), "http_status": exc.status_code},
    )
    return _envelope(exc.code, exc.message, exc.status_code)


async def handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    """Framework or router failure (404 on an unknown path, 405, and similar)."""
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    message = exc.detail if isinstance(exc.detail, str) else "Request could not be completed."
    return _envelope(code, message, exc.status_code)


async def handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    """Request validation failure. Reports the offending locations, never their values."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    fields = sorted(
        {".".join(str(part) for part in error.get("loc", ())[1:]) for error in exc.errors()}
    )
    detail = ", ".join(field for field in fields if field) or "request"
    return _envelope(
        ErrorCode.VALIDATION_ERROR,
        f"Request validation failed for: {detail}",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


async def handle_entitlement_error(_: Request, exc: Exception) -> JSONResponse:
    """A row-level authorization denial (task 4.3). Maps to 403, disclosing nothing (req 15.5)."""
    assert isinstance(exc, EntitlementError)  # noqa: S101
    _logger.info("entitlement denied")
    return _envelope(
        ErrorCode.ENTITLEMENT_DENIED,
        "you are not entitled to this resource",
        status.HTTP_403_FORBIDDEN,
    )


async def handle_authentication_error(_: Request, exc: Exception) -> JSONResponse:
    """An authentication failure raised below the middleware. Maps to 401, saying nothing of why."""
    assert isinstance(exc, AuthenticationError)  # noqa: S101
    _logger.info("authentication error", extra={"reason": type(exc).__name__})
    return _envelope(
        ErrorCode.UNAUTHENTICATED,
        "the access token is missing, invalid or expired",
        status.HTTP_401_UNAUTHORIZED,
    )


async def handle_audit_unavailable(_: Request, exc: Exception) -> JSONResponse:
    """The audit sink failed closed (task 4.6). The request is refused, not served unaudited."""
    assert isinstance(exc, AuditUnavailableError)  # noqa: S101
    _logger.warning("request failed closed for audit")
    return _envelope(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        "the request could not be audited and was not completed",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )


async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    """Anything unhandled. The cause goes to the (redacted) log, never to the client."""
    _logger.exception("unhandled error", extra={"error_type": type(exc).__name__})
    return _envelope(
        ErrorCode.INTERNAL_ERROR,
        "An unexpected error occurred. Quote the correlation ID when reporting it.",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler on the application."""
    app.add_exception_handler(ApiError, handle_api_error)
    app.add_exception_handler(EntitlementError, handle_entitlement_error)
    app.add_exception_handler(AuthenticationError, handle_authentication_error)
    app.add_exception_handler(AuditUnavailableError, handle_audit_unavailable)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
