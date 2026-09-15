"""Connection-management tests (task 1.1).

The load-bearing test here is :func:`test_foreign_keys_enforced_on_pooled_connection`. Asserting
that the ``connect`` listener is registered would pass even if the pragma never reached SQLite, so
enforcement is proven by provoking a real constraint violation on a connection that came out of the
pool — and then again on a *second* checkout, since the first physical connection is the easy case.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import QueuePool

from c360.data.engine import (
    BUSY_TIMEOUT_MS,
    CACHE_SIZE_KIB,
    MMAP_SIZE_BYTES,
    AccessMode,
    DatabaseFileMissingError,
    create_sqlite_engine,
    sqlite_uri,
)

# A parent/child pair is the smallest schema that can demonstrate FK enforcement.
_SCHEMA = (
    "CREATE TABLE parent (id TEXT PRIMARY KEY)",
    "CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES parent(id))",
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A database file with the fixture schema and one parent row, opened read-write to build."""
    path = tmp_path / "fixture.db"
    engine = create_sqlite_engine(path, mode=AccessMode.READ_WRITE_CREATE, pool_size=2)
    try:
        with engine.begin() as connection:
            for statement in _SCHEMA:
                connection.execute(text(statement))
            connection.execute(text("INSERT INTO parent (id) VALUES ('p1')"))
    finally:
        engine.dispose()
    return path


# ---------------------------------------------------------------- URI construction


def test_uri_is_absolute_and_encodes_spaces(tmp_path: Path) -> None:
    path = tmp_path / "a dir with spaces" / "customer.db"
    uri = sqlite_uri(path, AccessMode.READ_ONLY)

    assert uri.startswith("file:///")
    assert "%20" in uri
    assert " " not in uri
    assert uri.endswith("?mode=ro")


def test_uri_mode_reflects_access_mode(tmp_path: Path) -> None:
    path = tmp_path / "customer.db"

    assert sqlite_uri(path, AccessMode.READ_ONLY).endswith("?mode=ro")
    assert sqlite_uri(path, AccessMode.READ_WRITE).endswith("?mode=rw")
    assert sqlite_uri(path, AccessMode.READ_WRITE_CREATE).endswith("?mode=rwc")


def test_uri_round_trips_through_sqlite(tmp_path: Path) -> None:
    """The encoded URI has to be understood by SQLite itself, not just look plausible."""
    path = tmp_path / "spaced dir" / "customer.db"
    path.parent.mkdir()

    connection = sqlite3.connect(sqlite_uri(path, AccessMode.READ_WRITE_CREATE), uri=True)
    try:
        connection.execute("CREATE TABLE t (id INTEGER)")
    finally:
        connection.close()

    assert path.is_file()


def test_writable_property() -> None:
    assert not AccessMode.READ_ONLY.writable
    assert AccessMode.READ_WRITE.writable
    assert AccessMode.READ_WRITE_CREATE.writable


# ---------------------------------------------------------------- foreign keys


def test_foreign_keys_enforced_on_pooled_connection(db_path: Path) -> None:
    """`PRAGMA foreign_keys` is per-connection; prove it reached a pooled one."""
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_WRITE, pool_size=2)
    try:
        # First checkout: creates a physical connection.
        with engine.begin() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                connection.execute(
                    text("INSERT INTO child (id, parent_id) VALUES ('c1', 'missing')")
                )

        # Second checkout: returns the *recycled* connection from the pool. If the listener only
        # fired for brand-new connections, or if the failed transaction reset connection state,
        # enforcement would silently be gone here.
        with engine.begin() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                connection.execute(
                    text("INSERT INTO child (id, parent_id) VALUES ('c2', 'missing')")
                )

        # And a valid insert still succeeds, so the test is not passing because writes are broken.
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO child (id, parent_id) VALUES ('c3', 'p1')"))
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM child")).scalar_one() == 1
    finally:
        engine.dispose()


def test_foreign_keys_enforced_on_every_connection_in_the_pool(db_path: Path) -> None:
    """Hold every pooled connection open at once, so each is a distinct physical connection."""
    pool_size = 4
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_WRITE, pool_size=pool_size)
    try:
        connections = [engine.connect() for _ in range(pool_size)]
        try:
            for connection in connections:
                assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        finally:
            for connection in connections:
                connection.close()
    finally:
        engine.dispose()


# ---------------------------------------------------------------- pragmas


def test_connection_pragmas_match_design(db_path: Path) -> None:
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == BUSY_TIMEOUT_MS
            assert connection.execute(text("PRAGMA cache_size")).scalar_one() == CACHE_SIZE_KIB
            assert connection.execute(text("PRAGMA mmap_size")).scalar_one() == MMAP_SIZE_BYTES
            # temp_store: 0=default, 1=file, 2=memory
            assert connection.execute(text("PRAGMA temp_store")).scalar_one() == 2
    finally:
        engine.dispose()


def test_writable_engine_establishes_wal(db_path: Path) -> None:
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_WRITE, pool_size=1)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
    finally:
        engine.dispose()


def test_readonly_engine_inherits_wal_without_setting_it(db_path: Path) -> None:
    """A read-only reader must not attempt `journal_mode = WAL`; it inherits the file's mode."""
    writer = create_sqlite_engine(db_path, mode=AccessMode.READ_WRITE, pool_size=1)
    try:
        with writer.connect() as connection:
            connection.execute(text("PRAGMA journal_mode"))
    finally:
        writer.dispose()

    reader = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        with reader.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
    finally:
        reader.dispose()


def test_setting_wal_on_a_readonly_connection_fails(tmp_path: Path) -> None:
    """Documents *why* `journal_mode` is excluded from the read-only pragma set.

    The database is built without going through :func:`create_sqlite_engine`, so it is still in
    the default ``delete`` journal mode. That matters: on a file already in WAL the statement is a
    no-op and succeeds, which is precisely why the failure is easy to miss until the one deployment
    where a reader opens a freshly-copied non-WAL file.
    """
    path = tmp_path / "delete_mode.db"
    builder = sqlite3.connect(path)
    try:
        builder.execute("CREATE TABLE t (id INTEGER)")
        builder.commit()
        assert builder.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        builder.close()

    connection = sqlite3.connect(sqlite_uri(path, AccessMode.READ_ONLY), uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            connection.execute("PRAGMA journal_mode = WAL")
    finally:
        connection.close()

    # And the engine opens the same non-WAL file read-only without raising, because it does not
    # send the statement at all.
    engine = create_sqlite_engine(path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar_one() == "delete"
    finally:
        engine.dispose()


# ---------------------------------------------------------------- access mode


def test_readonly_engine_rejects_writes(db_path: Path) -> None:
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        with pytest.raises(OperationalError, match="readonly database"), engine.begin() as conn:
            conn.execute(text("INSERT INTO parent (id) VALUES ('p2')"))
    finally:
        engine.dispose()


def test_readonly_engine_reads(db_path: Path) -> None:
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id FROM parent")).scalar_one() == "p1"
    finally:
        engine.dispose()


def test_readonly_open_of_missing_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(DatabaseFileMissingError, match="does not exist"):
        create_sqlite_engine(tmp_path / "absent.db", mode=AccessMode.READ_ONLY)


def test_read_write_create_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "customer.db"
    engine = create_sqlite_engine(path, mode=AccessMode.READ_WRITE_CREATE, pool_size=1)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE t (id INTEGER)"))
    finally:
        engine.dispose()

    assert path.is_file()


# ---------------------------------------------------------------- pool shape


def test_pool_is_bounded_with_no_overflow(db_path: Path) -> None:
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=3)
    try:
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == 3
        assert engine.pool._max_overflow == 0
    finally:
        engine.dispose()


def test_pool_size_must_be_positive(db_path: Path) -> None:
    with pytest.raises(ValueError, match="pool_size must be at least 1"):
        create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=0)


def test_pool_reuses_connections_rather_than_opening_one_per_operation(db_path: Path) -> None:
    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=2)
    try:
        pool = engine.pool
        assert isinstance(pool, QueuePool)
        for _ in range(5):
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        # Five sequential checkouts against a pool of two must not have opened five connections.
        assert pool.checkedin() == 1
        assert pool.checkedout() == 0
    finally:
        engine.dispose()
