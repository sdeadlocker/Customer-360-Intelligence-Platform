"""SQLite engine construction and per-connection ``PRAGMA`` setup.

Design §4.2 lists the pragmas; design §12.3 fixes the concurrency model. Two details in there are
the reason this module exists rather than a bare :func:`sqlalchemy.create_engine` call at each call
site:

* ``foreign_keys`` and ``busy_timeout`` are **per-connection** settings. SQLite forgets both when a
  connection closes, so a pooled connection that was never configured enforces no foreign keys at
  all. Applying them from a ``connect`` event listener is the only place that covers every
  connection the pool ever makes, including ones created long after startup to replace a recycled
  one. Design §4.2 calls this out as "a common source of silently unenforced constraints", and
  :mod:`tests.test_engine` asserts enforcement on a connection taken from the pool rather than
  trusting that the listener was registered.
* ``journal_mode`` is **not** a per-connection setting. It is a property of the database file, so
  changing it requires write access and issuing it on a ``mode=ro`` connection raises
  ``attempt to write a readonly database`` — but only when the file is not *already* in WAL, which
  is what makes the mistake easy to ship. WAL is therefore established once by whoever opens the
  file for writing (migrations, the seeder) and simply inherited by every read-only reader. That is
  a deviation from a literal reading of task 1.1's pragma list, and the alternative — sending the
  statement anyway and swallowing the error — would mean a genuinely un-WAL-ed database looked
  identical to a healthy one.

Connections are made through an explicit ``creator`` rather than by encoding the path into a
SQLAlchemy URL. A SQLite *URI* filename has to survive two different parsers (SQLAlchemy's URL
parser, then SQLite's own), and on Windows the path carries a drive letter and, in this repository,
a space. Handing :func:`sqlite3.connect` the URI directly leaves exactly one parser in the path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import QueuePool

# ---------------------------------------------------------------- design §4.2 pragma values

#: Milliseconds a reader waits on a locked database before raising. Design §12.3: this guards the
#: brief writer window during ``POST /admin/recompute``.
BUSY_TIMEOUT_MS: Final = 5_000

#: Negative values are KiB rather than pages, so this is a 64 MiB page cache per connection.
CACHE_SIZE_KIB: Final = -64_000

#: 256 MiB memory-mapped I/O window.
MMAP_SIZE_BYTES: Final = 268_435_456

#: Pragmas that are per-connection state and must be re-applied on every physical connection.
_CONNECTION_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA foreign_keys = ON",
    f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}",
    f"PRAGMA cache_size = {CACHE_SIZE_KIB}",
    "PRAGMA temp_store = MEMORY",
    f"PRAGMA mmap_size = {MMAP_SIZE_BYTES}",
)

#: Pragmas that touch the database file and therefore need write access.
_WRITABLE_ONLY_PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
)


class AccessMode(StrEnum):
    """How a database file is opened.

    The values are SQLite URI ``mode=`` values, so they are usable directly in a URI.
    """

    #: Read-only. Fails if the file does not exist. The runtime mode for customer and knowledge
    #: databases (design §12.3).
    READ_ONLY = "ro"
    #: Read-write, failing if the file does not exist. For migrating an already-created database.
    READ_WRITE = "rw"
    #: Read-write, creating the file when absent. Migrations and the seeder.
    READ_WRITE_CREATE = "rwc"

    @property
    def writable(self) -> bool:
        return self is not AccessMode.READ_ONLY


class DatabaseFileMissingError(FileNotFoundError):
    """Raised when a read-only open is asked for a database file that does not exist.

    SQLite's own error for this is ``unable to open database file``, which is equally consistent
    with a permissions problem or a bad directory. Since a missing file is the overwhelmingly
    likely cause before the seeder has run, it is worth naming.
    """


def sqlite_uri(path: Path, mode: AccessMode = AccessMode.READ_ONLY) -> str:
    """Return a SQLite URI filename for ``path``.

    ``file:`` URIs are the only way to request read-only access, which is the whole point of §12.3.
    The path is percent-encoded because this repository's own checkout contains a space, and an
    unencoded space silently truncates the filename SQLite sees.
    """
    posix = path.resolve().as_posix()
    # POSIX absolute paths already start with '/'; Windows paths start with a drive letter and
    # need one added so the authority-less 'file://' form comes out as 'file:///C:/...'.
    if not posix.startswith("/"):
        posix = f"/{posix}"
    return f"file://{quote(posix, safe='/:')}?mode={mode.value}"


def _apply_pragmas(connection: sqlite3.Connection, *, writable: bool) -> None:
    """Apply the design §4.2 pragmas to one physical connection."""
    cursor = connection.cursor()
    try:
        for statement in _CONNECTION_PRAGMAS:
            cursor.execute(statement)
        if writable:
            for statement in _WRITABLE_ONLY_PRAGMAS:
                cursor.execute(statement)
    finally:
        cursor.close()


def _make_creator(path: Path, mode: AccessMode) -> Callable[[], sqlite3.Connection]:
    uri = sqlite_uri(path, mode)

    def _connect() -> sqlite3.Connection:
        # check_same_thread=False because a pooled connection is checked out by whichever worker
        # thread needs it. That is safe here and not a relaxation of SQLite's threading rules: the
        # pool guarantees a single concurrent user per connection, which is the invariant
        # check_same_thread exists to approximate.
        return sqlite3.connect(
            uri,
            uri=True,
            check_same_thread=False,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )

    return _connect


def create_sqlite_engine(
    path: Path,
    *,
    mode: AccessMode = AccessMode.READ_ONLY,
    pool_size: int = 8,
) -> Engine:
    """Build an :class:`~sqlalchemy.Engine` over a SQLite file.

    Args:
        path: Database file. Resolved to an absolute path before use.
        mode: Access mode. Defaults to read-only, because that is the runtime case and the safe
            default: an accidental omission produces a failed write, not a mutated source database.
        pool_size: Bounded pool size. Design §12.3 wants one connection per worker thread
            (roughly twice the CPU count, configured as ``SQLITE_READ_POOL_SIZE``), not one per
            request.

    Raises:
        DatabaseFileMissingError: ``mode`` is read-only and ``path`` does not exist.
        ValueError: ``pool_size`` is not positive.
    """
    if pool_size < 1:
        raise ValueError(f"pool_size must be at least 1, got {pool_size}")

    resolved = path.resolve()
    if not mode.writable and not resolved.is_file():
        raise DatabaseFileMissingError(
            f"cannot open {resolved} read-only: file does not exist. "
            "Run migrations and the seeder first."
        )
    if mode is AccessMode.READ_WRITE_CREATE:
        resolved.parent.mkdir(parents=True, exist_ok=True)

    writable = mode.writable
    engine = create_engine(
        # The URL carries the dialect only. `creator` supplies the connection, so nothing about
        # the filesystem path passes through SQLAlchemy's URL parsing.
        "sqlite://",
        creator=_make_creator(resolved, mode),
        poolclass=QueuePool,
        pool_size=pool_size,
        # No overflow: the bound in §12.3 is the point. An overflow connection would silently
        # exceed the per-instance file-handle and page-cache budget under load.
        max_overflow=0,
        pool_pre_ping=False,
        # A read-only SQLite file has no server to time a connection out, so recycling would only
        # discard a warm page cache.
        pool_recycle=-1,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        _apply_pragmas(dbapi_connection, writable=writable)

    return engine
