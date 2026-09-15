"""The API-boundary masking helper (task 4.4).

Every customer-data response is built through :func:`masked_envelope`, never by wrapping a model in
:meth:`Envelope.of` directly. That is the mechanism design §7.2 asks for — masking "applied ... so
no route handler can forget it" — expressed as the one function a handler calls to turn a domain
model into a response. It applies the caller's field policy, drops or transforms the sensitive
fields, records what it masked in ``meta.masked_fields``, and annotates the span with the field
groups it touched (design §13.4's masking-applied signal).

The masked data is carried in the envelope as a plain dict rather than the original typed model:
the whole point is that the payload no longer *is* the model — it is the subset and the derived
forms this role is allowed to see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel

from c360.api.envelope import Envelope
from c360.core.telemetry import SpanAttr
from c360.security.serializer import mask_model

if TYPE_CHECKING:
    from datetime import datetime

    from c360.security.model import Principal


def masked_envelope(
    model: BaseModel,
    principal: Principal,
    *,
    as_of: datetime | None = None,
) -> Envelope[dict[str, Any]]:
    """Serialize ``model`` under ``principal``'s field policy into a masked envelope.

    ``meta.masked_fields`` lists every field that was hidden, banded or partially rendered, dotted
    for nested paths (``account.balance_cents``), so the UI can render a "restricted" affordance
    without guessing.
    """
    data, masked_fields = mask_model(model, principal.field_policy)
    _annotate_span(masked_fields)
    return Envelope.of(data, as_of=as_of, masked_fields=masked_fields)


def _annotate_span(masked_fields: list[str]) -> None:
    """Record that masking ran and how much, without putting field *values* on the span."""
    span = trace.get_current_span()
    if span.get_span_context().is_valid:
        # The count, not the names of customer-specific paths — a bounded, non-identifying signal.
        span.set_attribute(SpanAttr.MASKING_APPLIED, len(masked_fields))
