"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}
"""

from __future__ import annotations

from c360.data.ddl import Statements, drop_tables, execute_all

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


# ---------------------------------------------------------------- DDL
_UPGRADE: Statements = ()

#: Child-before-parent order; see :func:`c360.data.ddl.drop_tables`.
_DROP_ORDER: Statements = ()


def upgrade() -> None:
    execute_all(_UPGRADE)


def downgrade() -> None:
    drop_tables(_DROP_ORDER)
