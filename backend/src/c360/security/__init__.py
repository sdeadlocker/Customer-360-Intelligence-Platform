"""Security foundation (Phase 4).

Authentication, authorization, field masking, audit and graph redaction. Built before the customer
API surface, because retrofitting masking onto existing endpoints is how leaks happen (task 4).

The public vocabulary — :class:`Role`, :class:`Principal`, :class:`EntitlementScope`,
:class:`FieldPolicy`, :class:`MaskMode` — lives in :mod:`c360.security.model` so that services,
tools and the API all name the same types.
"""

from __future__ import annotations

from c360.security.authorization import (
    authorize_customer,
    is_customer_visible,
)
from c360.security.entitlement import (
    AllScope,
    BookScope,
    EntitlementScope,
    SegmentScope,
    customer_predicate,
    scope_from_claim,
)
from c360.security.errors import (
    AuditUnavailableError,
    AuthenticationError,
    EntitlementError,
    SecurityError,
)
from c360.security.graph_redaction import (
    is_node_restricted,
    redact_node,
    redact_view,
)
from c360.security.model import (
    KNOWLEDGE_LEVELS_BY_ROLE,
    FieldGroup,
    KnowledgeLevel,
    MaskMode,
    Principal,
    Role,
)
from c360.security.policy import FieldPolicy, policy_for_role
from c360.security.serializer import mask_model

__all__ = [
    "KNOWLEDGE_LEVELS_BY_ROLE",
    "AllScope",
    "AuditUnavailableError",
    "AuthenticationError",
    "BookScope",
    "EntitlementError",
    "EntitlementScope",
    "FieldGroup",
    "FieldPolicy",
    "KnowledgeLevel",
    "MaskMode",
    "Principal",
    "Role",
    "SecurityError",
    "SegmentScope",
    "authorize_customer",
    "customer_predicate",
    "is_customer_visible",
    "is_node_restricted",
    "mask_model",
    "policy_for_role",
    "redact_node",
    "redact_view",
    "scope_from_claim",
]
