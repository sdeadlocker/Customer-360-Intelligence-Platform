"""Shared query helpers for the Phase 5 service and API tests.

The ``phase5_db`` / ``phase5_engine`` *fixtures* live in :mod:`tests.conftest` so pytest discovers
them by name (importing a fixture into each module would trip the redefinition lint on every use).
This module holds only the plain, importable helpers those tests share — small query shims and a
principal builder — which are functions, not fixtures, and so import cleanly.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, text

from c360.security.entitlement import AllScope, BookScope, SegmentScope
from c360.security.model import Principal, Role
from c360.security.policy import policy_for_role

#: Kept in step with the constants in :mod:`tests.conftest` so a test can reference the panel shape.
PANEL_COUNT = 40
PANEL_SEED = 42


def query_all(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql), params or {}).all())


def query_one(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    rows = query_all(engine, sql, params)
    return rows[0] if rows else None


def all_customer_ids(engine: Engine) -> list[str]:
    return [
        str(row[0])
        for row in query_all(engine, "SELECT customer_id FROM customer ORDER BY customer_id")
    ]


def principal_for(
    role: Role,
    scope: AllScope | BookScope | SegmentScope | None = None,
) -> Principal:
    """A principal with the given role and scope; defaults to seeing everything."""
    return Principal(
        user_id=f"{role.value.lower()}.tester",
        role=role,
        entitlement=scope or AllScope(),
        field_policy=policy_for_role(role),
        knowledge_levels=frozenset(),
    )
