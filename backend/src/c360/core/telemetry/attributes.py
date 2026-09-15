"""Span attribute names owned by this application, and the pseudonymous customer reference.

Attribute names live in one place because the allowlist (design §13.4) is keyed on them. A typo in
an attribute name would otherwise mean the attribute is silently dropped at export — a failure mode
that is hard to notice and easy to prevent.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final


class SpanAttr:
    """Application-owned span attribute names, all under the ``c360.`` namespace."""

    CORRELATION_ID: Final = "c360.correlation_id"
    CUSTOMER_HASH: Final = "c360.customer_hash"
    ROLE: Final = "c360.role"
    SEGMENT: Final = "c360.segment"
    OUTCOME: Final = "c360.outcome"
    DEGRADED: Final = "c360.degraded"
    CACHE_HIT: Final = "c360.cache_hit"

    AGENT: Final = "c360.agent"
    AGENT_WAVE: Final = "c360.agent.wave"
    PROMPT_VERSION: Final = "c360.prompt_version"
    FORMULA_VERSION: Final = "c360.formula_version"
    CLAIM_VALIDATION: Final = "c360.claim_validation"

    TOOL: Final = "c360.tool"
    DOMAIN: Final = "c360.domain"

    DB_STATEMENT_ID: Final = "c360.db.statement_id"
    DB_ROW_COUNT: Final = "c360.db.row_count"
    DB_POOL_WAIT_MS: Final = "c360.db.pool_wait_ms"

    GRAPH_HOPS: Final = "c360.graph.hops"
    GRAPH_NODES_VISITED: Final = "c360.graph.nodes_visited"
    GRAPH_TRUNCATED: Final = "c360.graph.truncated"

    RETRIEVAL_STAGE: Final = "c360.retrieval.stage"
    RETRIEVAL_CANDIDATES: Final = "c360.retrieval.candidate_count"
    RETRIEVAL_RESULTS: Final = "c360.retrieval.result_count"
    RETRIEVAL_RERANKED: Final = "c360.retrieval.reranked"
    RETRIEVAL_ZERO_RESULT: Final = "c360.retrieval.zero_result"

    AUTHZ_DECISION: Final = "c360.authz.decision"
    MASKING_APPLIED: Final = "c360.masking.applied_groups"
    AUDIT_QUEUE_DEPTH: Final = "c360.audit.queue_depth"

    REPORT_TYPE: Final = "c360.report.type"
    REPORT_CUSTOMERS_RENDERED: Final = "c360.report.customers_rendered"
    REPORT_DEGRADED: Final = "c360.report.degraded"


class CustomerHasher:
    """Produce the salted pseudonymous customer reference required by requirement 18.9.

    A raw ``customer_id`` in a trace is customer data in a system with weaker access control than
    the application. An HMAC keyed on a deployment-scoped salt keeps a trace joinable to itself
    without being resolvable by whoever can read the tracing backend.
    """

    __slots__ = ("_enabled", "_salt")

    def __init__(self, salt: str) -> None:
        self._salt = salt.encode("utf-8")
        self._enabled = bool(salt.strip())

    @property
    def enabled(self) -> bool:
        """Whether a salt was configured. Without one, no reference is emitted at all."""
        return self._enabled

    def hash(self, customer_id: str) -> str | None:
        """Return a 16-character pseudonym, or ``None`` when hashing is disabled."""
        if not self._enabled:
            return None
        digest = hmac.new(self._salt, customer_id.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()[:16]
