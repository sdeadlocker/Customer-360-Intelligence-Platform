"""The row-level authorization gate, enforced above the repository (task 4.3).

Design §7.2's first gate: given a principal and a target customer, decide whether the request may
proceed at all. The decision itself is made in SQL by the repository (so a restricted book never
loses a match — see :mod:`c360.data.repositories.customer`); this module turns the boolean that
comes back into the right outcome and the right span/audit signal.

The 403-versus-404 rule (requirement 15.5) is the subtle part. A principal that is *not entitled* to
a customer that *exists* gets 403; a customer that *does not exist* gets 404. But the two must be
indistinguishable to a principal probing for customers outside its book — otherwise the error code
becomes an existence oracle. :meth:`CustomerRepository.visible_to` collapses both into one boolean
precisely so this function never has to ask "does it exist" separately and never opens that window.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from opentelemetry import trace

from c360.core.telemetry import CustomerHasher, SpanAttr
from c360.security.errors import EntitlementError

if TYPE_CHECKING:
    from c360.data.repositories.customer import SqliteCustomerRepository
    from c360.security.model import Principal


def authorize_customer(
    principal: Principal,
    customer_id: str,
    repository: SqliteCustomerRepository,
) -> None:
    """Raise unless ``principal`` may access ``customer_id``.

    Uses the scoped existence check so entitlement and existence are one question. On denial the
    span records the decision and the caller (or the audit middleware) writes a ``DENIED`` record;
    the exception carries no hint of whether the customer exists.

    Raises:
        EntitlementError: the customer does not exist, or the principal is not entitled to it. The
            API layer maps this onto 404 for a genuinely absent customer and 403 otherwise — a
            distinction it can only draw for a customer the caller is *already* entitled to, which
            is why a non-entitled probe uniformly cannot tell the two apart.
    """
    _record_customer_hash(customer_id)
    if repository.visible_to(principal.entitlement, customer_id):
        _record_decision("allow")
        return
    _record_decision("deny")
    _record_denied_metric(principal)
    raise EntitlementError("not entitled to this customer")


def is_customer_visible(
    principal: Principal,
    customer_id: str,
    repository: SqliteCustomerRepository,
) -> bool:
    """Non-raising form of :func:`authorize_customer`, for the graph redaction filter (task 4.7).

    The graph filter needs to *keep* a non-entitled node as structure-only rather than reject the
    whole request, so it asks this question instead of catching an exception.
    """
    return repository.visible_to(principal.entitlement, customer_id)


def _record_decision(decision: str) -> None:
    span = trace.get_current_span()
    if span.get_span_context().is_valid:
        span.set_attribute(SpanAttr.AUTHZ_DECISION, decision)


def _record_customer_hash(customer_id: str) -> None:
    """Attach the salted pseudonymous customer reference to the active span (requirement 18.9).

    A raw customer id in a trace is customer data in a backend with weaker access control than the
    app, so the trace carries an HMAC pseudonym instead — joinable to itself, not resolvable by
    whoever reads the tracing UI. When no salt is configured the hasher yields ``None`` and no
    attribute is set at all, which the startup advisory already warns about.
    """
    span = trace.get_current_span()
    if not span.get_span_context().is_valid:
        return
    pseudonym = _customer_hasher().hash(customer_id)
    if pseudonym is not None:
        span.set_attribute(SpanAttr.CUSTOMER_HASH, pseudonym)


@functools.lru_cache(maxsize=1)
def _customer_hasher() -> CustomerHasher:
    """The process-wide hasher, built once from the configured salt."""
    from c360.core.config import get_settings  # noqa: PLC0415 - avoid import cycle at load

    return CustomerHasher(get_settings().telemetry_customer_hash_salt)


def _record_denied_metric(principal: Principal) -> None:
    """Increment the denied-access counter, labelled by role only (task 10.2, design §13.3).

    Role is the one dimension the security dashboard needs; the customer id is never a label
    (design §13.4). The denial is also written to the audit log by the request gate — this counter
    is the aggregate the denied-access spike alert (task 10.4) burns on.
    """
    from c360.core.telemetry import get_metrics  # noqa: PLC0415 - avoid import cycle at load

    metrics = get_metrics()
    if metrics is not None:
        metrics.record_denied_access(role=str(principal.role))
