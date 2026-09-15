"""Loading the ``sqlite-vec`` extension onto a SQLite connection (task 7.2).

``kb_chunk_vec`` is a ``vec0`` virtual table, and ``vec0`` is a *loadable* SQLite extension rather
than a compiled-in one like FTS5. That has two consequences this module exists to handle:

* The extension must be loaded on **every** physical connection that touches ``knowledge.db`` —
  the one that creates the schema, the one the ingestion writer uses, and every pooled read
  connection at runtime. A ``vec0`` table cannot even be *opened* on a connection that has not
  loaded the extension, so a missing load surfaces as ``no such module: vec0`` on the first query,
  not at connect time. Loading from a connection event listener (see :mod:`c360.knowledge.engine`)
  is the only place that covers pooled connections created long after startup.
* ``enable_load_extension`` is a security-sensitive switch — it lets SQL load arbitrary shared
  objects — so it is turned on only for the moment the trusted, first-party ``sqlite-vec`` library
  is loaded and turned straight back off. No untrusted SQL ever runs on a connection with loading
  enabled.

The Python ``sqlite3`` build must have been compiled with ``enable_load_extension`` support. The
CPython builds this project pins have it; :func:`vec_available` reports the fact so readiness and a
startup check can surface a build that does not rather than failing on the first retrieval.
"""

from __future__ import annotations

import sqlite3
from typing import Final

import sqlite_vec

from c360.core.logging import get_logger

_logger = get_logger(__name__)

#: The module name a ``vec0`` virtual table is created ``USING``. Named for the error check below.
_VEC_MODULE: Final = "vec0"


class VecExtensionError(RuntimeError):
    """Raised when the ``sqlite-vec`` extension cannot be loaded onto a connection.

    Named rather than left as the bare ``sqlite3.OperationalError`` because the two likely causes —
    a Python built without ``enable_load_extension`` support, and a ``sqlite-vec`` wheel missing for
    the platform — are both deployment faults worth surfacing distinctly from a query error.
    """


def load_vec(connection: sqlite3.Connection) -> None:
    """Load ``sqlite-vec`` onto ``connection``, enabling extension loading only for the load itself.

    Raises:
        VecExtensionError: the Python ``sqlite3`` build does not support loadable extensions, or the
            ``sqlite-vec`` shared object could not be loaded.
    """
    try:
        connection.enable_load_extension(True)
    except AttributeError as exc:
        # `enable_load_extension` is absent when Python's sqlite3 was compiled without support.
        raise VecExtensionError(
            "the Python sqlite3 build does not support loadable extensions, "
            "which sqlite-vec requires"
        ) from exc
    try:
        sqlite_vec.load(connection)
    except sqlite3.OperationalError as exc:
        raise VecExtensionError(f"could not load sqlite-vec: {exc}") from exc
    finally:
        # Always turn loading back off, even if the load raised, so no later statement on this
        # connection can load an extension.
        connection.enable_load_extension(False)


def vec_available() -> bool:
    """Whether ``sqlite-vec`` can be loaded and answers a query in this environment.

    Used by the startup verification (task 7.2) and readiness. Opens a throwaway in-memory
    connection so the check is side-effect-free and needs no database file.
    """
    connection = sqlite3.connect(":memory:")
    try:
        load_vec(connection)
        connection.execute("SELECT vec_version()").fetchone()
        return True
    except (VecExtensionError, sqlite3.Error) as exc:
        _logger.warning("sqlite-vec is not available", extra={"error_type": type(exc).__name__})
        return False
    finally:
        connection.close()


def vec_version() -> str | None:
    """The loaded ``sqlite-vec`` version string (e.g. ``v0.1.9``), or ``None`` if unavailable.

    Reported in readiness detail so an operator can see which extension build is in play without it
    being a health signal in itself.
    """
    connection = sqlite3.connect(":memory:")
    try:
        load_vec(connection)
        row = connection.execute("SELECT vec_version()").fetchone()
        return str(row[0]) if row else None
    except (VecExtensionError, sqlite3.Error):
        return None
    finally:
        connection.close()


__all__ = ["VecExtensionError", "load_vec", "vec_available", "vec_version"]
