"""Audit wiring at the API boundary (task 4.6).

Turns "who did what to whom" — a :class:`Principal`, an action, an outcome, the ambient correlation
and trace IDs — into an :class:`AuditRecord` and submits it to the writer, failing the request
closed if the writer cannot accept it (design §7.4).

Every customer-data read, agent run, unmask and export routes its audit write through
:func:`record_access`, so the mandatory-audit requirement (12.6) is met in one place rather than
per handler. The record carries the *field manifest* — which fields were accessed — not their
values; an audit log names what was seen, never reproduces it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from c360.core.context import get_correlation_id
from c360.core.telemetry import current_trace_id
from c360.security.audit import AuditOutcome, AuditRecord, now_iso
from c360.security.errors import AuditUnavailableError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.domain.ports import AuditSink
    from c360.security.model import Principal


def record_access(
    sink: AuditSink,
    principal: Principal,
    *,
    action: str,
    outcome: AuditOutcome,
    customer_id: str | None = None,
    fields_accessed: Sequence[str] | None = None,
    source_ip: str | None = None,
    request_path: str | None = None,
    agent_name: str | None = None,
    model_id: str | None = None,
    prompt_field_manifest: Sequence[str] | None = None,
    retrieved_doc_ids: Sequence[str] | None = None,
) -> None:
    """Build and submit an audit record, failing closed if the sink cannot accept it.

    Raises:
        AuditUnavailableError: the audit queue is saturated. The API maps this onto 503 and the
            request is not completed — serving customer data with no audit trail is the one outcome
            the subsystem exists to prevent.
    """
    record = AuditRecord(
        occurred_at=now_iso(),
        actor_user_id=principal.user_id,
        actor_role=str(principal.role),
        action=action,
        outcome=outcome,
        correlation_id=get_correlation_id() or "unbound",
        customer_id=customer_id,
        fields_accessed=_as_json(fields_accessed),
        source_ip=source_ip,
        trace_id=current_trace_id(),
        request_path=request_path,
        agent_name=agent_name,
        model_id=model_id,
        prompt_field_manifest=_as_json(prompt_field_manifest),
        retrieved_doc_ids=_as_json(retrieved_doc_ids),
    )
    if not sink.submit(record):
        raise AuditUnavailableError


def _as_json(values: Sequence[str] | None) -> str | None:
    """Serialize a string list to a compact JSON array, or ``None``, per the json_valid CHECK."""
    if values is None:
        return None
    return json.dumps(list(values), separators=(",", ":"))
