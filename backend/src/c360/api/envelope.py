"""The uniform response envelope and error envelope from design §6.2 and §6.3.

Every successful response is ``{"data": ..., "meta": {...}}``. ``meta`` is not decoration:

* ``masked_fields`` lets the UI render a "restricted" affordance without guessing why a value is
  absent, and lets an auditor see what a role actually received (requirement 12.4).
* ``errors`` carries per-domain failures so the aggregator can return a partial 200 instead of
  failing the whole dashboard (requirement 4.4).
* ``trace_id`` takes a support engineer from a user report straight to the trace (requirement 18.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from c360.core.context import require_correlation_id
from c360.core.telemetry import current_trace_id


class ErrorCode(StrEnum):
    """The complete error code set from design §6.3."""

    UNAUTHENTICATED = "UNAUTHENTICATED"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Error code to HTTP status, so a handler never has to remember the pairing.
ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.ENTITLEMENT_DENIED: 403,
    ErrorCode.CUSTOMER_NOT_FOUND: 404,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UPSTREAM_UNAVAILABLE: 503,
    ErrorCode.AGENT_TIMEOUT: 504,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ModuleError(BaseModel):
    """A single domain module's failure, surfaced without failing the whole response."""

    model_config = ConfigDict(frozen=True)

    module: str
    code: ErrorCode
    message: str


class Meta(BaseModel):
    """Envelope metadata."""

    model_config = ConfigDict(frozen=True)

    correlation_id: str
    trace_id: str | None = None
    as_of: datetime
    masked_fields: list[str] = Field(default_factory=list)
    computed_fields: list[str] = Field(default_factory=list)
    errors: list[ModuleError] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        as_of: datetime | None = None,
        masked_fields: list[str] | None = None,
        computed_fields: list[str] | None = None,
        errors: list[ModuleError] | None = None,
    ) -> Self:
        """Construct metadata from the ambient request context."""
        return cls(
            correlation_id=require_correlation_id(),
            trace_id=current_trace_id(),
            as_of=as_of or datetime.now(tz=UTC),
            masked_fields=masked_fields or [],
            computed_fields=computed_fields or [],
            errors=errors or [],
        )


class Envelope[DataT](BaseModel):
    """The standard success envelope."""

    model_config = ConfigDict(frozen=True)

    data: DataT
    meta: Meta

    @classmethod
    def of(
        cls,
        data: DataT,
        *,
        as_of: datetime | None = None,
        masked_fields: list[str] | None = None,
        computed_fields: list[str] | None = None,
        errors: list[ModuleError] | None = None,
    ) -> Self:
        """Wrap a payload with metadata drawn from the ambient request context."""
        return cls(
            data=data,
            meta=Meta.build(
                as_of=as_of,
                masked_fields=masked_fields,
                computed_fields=computed_fields,
                errors=errors,
            ),
        )


class ErrorBody(BaseModel):
    """The body of an error envelope. Messages never include field values (design §6.3)."""

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    message: str
    correlation_id: str


class ErrorEnvelope(BaseModel):
    """The uniform error envelope."""

    model_config = ConfigDict(frozen=True)

    error: ErrorBody

    @classmethod
    def of(cls, code: ErrorCode, message: str) -> Self:
        return cls(
            error=ErrorBody(
                code=code,
                message=message,
                correlation_id=require_correlation_id(),
            )
        )


class ApiError(Exception):
    """Raised by application code to produce a coded error envelope.

    Carrying the code rather than the status means the HTTP mapping lives in exactly one place, and
    a handler cannot invent a status the API contract does not document.
    """

    __slots__ = ("code", "detail", "message")

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def status_code(self) -> int:
        return ERROR_STATUS[self.code]
