"""Security-layer exceptions (Phase 4).

Kept in their own module so both the provider and the middleware can raise and catch them without a
circular import, and so the API layer can map them onto coded envelopes in one place.

Two rules govern every message here:

* an authentication failure never says *why*. "expired" versus "bad signature" versus "unknown
  key" are all the same 401 to a caller; distinguishing them hands a probe a way to tell a forged
  token from a stale one.
* no message carries a token, a claim value or a customer identifier — the same redaction rule
  requirement 18.8 applies to logs applies to an exception that will end up in one.
"""

from __future__ import annotations


class SecurityError(RuntimeError):
    """Base for every failure raised by the security layer."""


class AuthenticationError(SecurityError):
    """A token could not be accepted. Maps to 401 (requirement 12.1).

    The default message is intentionally uniform; callers may pass a more specific one for the
    (redacted) log, but the API never forwards it to the client verbatim.
    """

    def __init__(self, message: str = "authentication failed") -> None:
        super().__init__(message)


class EntitlementError(SecurityError):
    """The principal is authenticated but not entitled to the resource. Maps to 403.

    Requirement 15.5 keeps 403 (not entitled) distinct from 404 (does not exist); this exception is
    the 403 half and is raised only after existence has been confirmed.
    """

    def __init__(self, message: str = "not entitled") -> None:
        super().__init__(message)


class AuditUnavailableError(SecurityError):
    """The audit sink could not accept a record, so the request must fail closed (design §7.4).

    Requirement 12.6 makes the audit write mandatory; proceeding without it would serve customer
    data with no record that it happened. Maps to 503 — the condition is transient (a saturated
    queue drains) rather than a client error.
    """

    def __init__(self, message: str = "audit subsystem unavailable") -> None:
        super().__init__(message)
