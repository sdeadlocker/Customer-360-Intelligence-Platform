"""SQLite engine for the knowledge database (task 7.2).

This is :func:`c360.data.engine.create_sqlite_engine` with one addition: every physical connection
loads the ``sqlite-vec`` extension from the ``connect`` event listener, because the ``kb_chunk_vec``
virtual table cannot be opened otherwise (see :mod:`c360.knowledge.extension`). The customer engine
does not need the extension, so rather than complicate that hot path this module wraps it.

Runtime opens ``knowledge.db`` read-only, exactly like the customer database (design §9.3). The
ingestion pipeline opens it read-write-create to build it. Both go through here so the pragma set
and the extension load are identical whichever side is talking to the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event

from c360.data.engine import AccessMode, create_sqlite_engine
from c360.knowledge.extension import load_vec


def create_knowledge_engine(
    path: Path,
    *,
    mode: AccessMode = AccessMode.READ_ONLY,
    pool_size: int = 8,
) -> Engine:
    """Build an engine over the knowledge database with ``sqlite-vec`` loaded on every connection.

    Args:
        path: The ``knowledge.db`` file. Resolved to absolute before use.
        mode: Access mode; defaults to read-only, the runtime case.
        pool_size: Bounded pool size, as for the customer engine.

    Raises:
        DatabaseFileMissingError: ``mode`` is read-only and ``path`` does not exist.
        VecExtensionError: ``sqlite-vec`` cannot be loaded in this environment (surfaced on the
            first connection the pool opens).
    """
    engine = create_sqlite_engine(path, mode=mode, pool_size=pool_size)

    @event.listens_for(engine, "connect")
    def _load_vec(dbapi_connection: Any, _record: Any) -> None:
        # Runs after the pragma listener registered by create_sqlite_engine — SQLAlchemy fires
        # connect listeners in registration order, and the extension load does not depend on any
        # pragma, so the order is immaterial here.
        load_vec(dbapi_connection)

    return engine


__all__ = ["create_knowledge_engine"]
