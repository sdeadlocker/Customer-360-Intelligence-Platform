"""SQLite data layer: connections, schema, value types and repository adapters.

The layer exists so that the rest of the application never sees SQLite. Design §12.3 is explicit
that SQLite is the one place the storage choice constrains the architecture, and that repository
ports are what make a PostgreSQL adapter a swap rather than a rewrite.
"""

from __future__ import annotations

from c360.data.engine import (
    AccessMode,
    DatabaseFileMissingError,
    create_sqlite_engine,
    sqlite_uri,
)
from c360.data.migrations import (
    MigrationError,
    current_revision,
    downgrade_database,
    head_revision,
    is_up_to_date,
    upgrade_database,
)

__all__ = [
    "AccessMode",
    "DatabaseFileMissingError",
    "MigrationError",
    "create_sqlite_engine",
    "current_revision",
    "downgrade_database",
    "head_revision",
    "is_up_to_date",
    "sqlite_uri",
    "upgrade_database",
]
