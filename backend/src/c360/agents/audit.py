"""Auditing every agent run (task 8.10, requirement 10.14, design §7.4).

Each agent run is written to the append-only audit log with *what* reached the model — the agent
name, the model id, the prompt version, the field manifest (the surviving allowlisted fields) and
the retrieved document ids — and never the field *values*. That is the audit contract: the manifest
answers "what data was dispatched to Bedrock for this customer" without the log itself becoming a
copy of the customer's data.

The runner calls an :class:`AgentAuditHook`; :func:`build_agent_audit_hook` binds one to the audit
sink and the request's principal and customer. The runner depends only on the callable, so it needs
no import of the audit subsystem and stays unit-testable with a plain function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from c360.api.audit import record_access
from c360.security.audit import AuditOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.domain.ports import AuditSink
    from c360.security.model import Principal


class AgentAuditHook(Protocol):
    """Called once per agent run to record what was dispatched to the model (task 8.10)."""

    def __call__(
        self,
        *,
        agent: str,
        model_id: str,
        prompt_version: str,
        field_manifest: Sequence[str],
        retrieved_doc_ids: Sequence[str],
        degraded: bool,
    ) -> None:
        """Persist the run's audit record."""
        ...


def build_agent_audit_hook(
    sink: AuditSink, principal: Principal, customer_id: str
) -> AgentAuditHook:
    """Bind an :class:`AgentAuditHook` to the audit sink for one principal + customer (task 8.10).

    The returned hook writes one ``AGENT_RUN`` record per agent. It is best-effort: an audit failure
    here is swallowed so a saturated queue degrades a *label* on a card, not the card itself — the
    fail-closed audit that actually gates AI access happens once, at the route, before any card is
    streamed. Recording the same fail-closed discipline per card would reject a whole dashboard on a
    transient queue spike, which is the wrong trade for a labelling record.
    """

    def _hook(
        *,
        agent: str,
        model_id: str,
        prompt_version: str,
        field_manifest: Sequence[str],
        retrieved_doc_ids: Sequence[str],
        degraded: bool,
    ) -> None:
        # The prompt version and degraded flag ride in the field manifest's companion columns; the
        # audit schema (task 4.6) has agent_name, model_id, prompt_field_manifest and
        # retrieved_doc_ids, which is exactly what a run needs to be reconstructed.
        manifest = [*field_manifest, f"prompt_version={prompt_version}", f"degraded={degraded}"]
        try:
            record_access(
                sink,
                principal,
                action="AGENT_RUN",
                outcome=AuditOutcome.ALLOWED,
                customer_id=customer_id,
                agent_name=agent,
                model_id=model_id,
                prompt_field_manifest=manifest,
                retrieved_doc_ids=list(retrieved_doc_ids) or None,
            )
        except Exception:
            return

    return _hook


__all__ = ["AgentAuditHook", "build_agent_audit_hook"]
