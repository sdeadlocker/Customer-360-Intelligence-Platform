"""Programmatic access to the Alembic migration set (task 1.2).

The Alembic CLI is fine for a developer at a prompt, but three callers need migrations from inside
the process: the seeder (task 2.8, which must create the database before writing to it), the test
suite (which migrates a fresh ``tmp_path`` file per test), and readiness (requirement 18.14, which
reports the schema version the process is serving). Shelling out to ``alembic`` from any of those
would mean a subprocess, a different working directory and a second path-resolution rule.

Everything here goes through the same ``migrations/env.py`` the CLI uses, so there is exactly one
code path that applies a migration.

:func:`current_revision` deliberately opens the file **read-only**. Asking "which revision is this
database at" must not be able to create the file, upgrade it, or take a write lock on it — a
readiness probe running that check against a live database every few seconds would otherwise be
contending with the audit writer for no reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from c360.core.logging import get_logger
from c360.data.engine import AccessMode, create_sqlite_engine

_logger = get_logger(__name__)

# src/c360/data/migrations.py -> data -> c360 -> src -> backend
BACKEND_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
ALEMBIC_INI: Final[Path] = BACKEND_ROOT / "alembic.ini"
MIGRATIONS_DIR: Final[Path] = BACKEND_ROOT / "migrations"

#: Alembic's bookkeeping table. Named here because :func:`current_revision` probes for it directly
#: rather than relying on Alembic internals to tolerate its absence.
VERSION_TABLE: Final = "alembic_version"


class MigrationError(RuntimeError):
    """Raised when the migration environment itself is unusable."""


def alembic_config(db_path: Path) -> Config:
    """Build an Alembic :class:`Config` targeting ``db_path``.

    ``script_location`` is overridden with an absolute path. Alembic resolves the value from
    ``alembic.ini`` relative to the process working directory, so leaving it relative would work
    when run from ``backend/`` and fail from the repository root — which is where ``make`` runs.
    """
    if not ALEMBIC_INI.is_file():
        raise MigrationError(f"alembic.ini not found at {ALEMBIC_INI}")

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Read by `env.py`. Takes precedence over -x, the environment and Settings, because a caller
    # holding a Path is being explicit and should not be overridden by ambient configuration.
    config.attributes["db_path"] = str(db_path)
    return config


def head_revision() -> str:
    """Return the head revision of the migration set on disk.

    Read from the scripts rather than hardcoded, so adding a migration cannot leave a stale
    constant behind for readiness to compare against.
    """
    script = ScriptDirectory.from_config(alembic_config(Path("unused")))
    head = script.get_current_head()
    if head is None:
        raise MigrationError(f"no migrations found in {MIGRATIONS_DIR}")
    return head


def upgrade_database(db_path: Path, revision: str = "head") -> None:
    """Apply migrations up to ``revision``, creating the database file if absent."""
    # Imported here rather than at module scope: `alembic.command` pulls in the whole Alembic
    # command surface, and `current_revision` — the hot path for readiness — needs none of it.
    from alembic import command  # noqa: PLC0415

    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), revision)
    _logger.info(
        "database migrated",
        extra={"revision": revision, "schema_version": current_revision(db_path)},
    )


def downgrade_database(db_path: Path, revision: str = "base") -> None:
    """Revert migrations down to ``revision``. ``base`` removes the whole schema."""
    from alembic import command  # noqa: PLC0415

    command.downgrade(alembic_config(db_path), revision)


def current_revision(db_path: Path) -> str | None:
    """Return the revision ``db_path`` is at, or ``None`` if it is unmigrated or absent."""
    if not db_path.is_file():
        return None

    engine = create_sqlite_engine(db_path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
                {"name": VERSION_TABLE},
            ).scalar_one_or_none()
            if exists is None:
                return None
            revision: str | None = connection.execute(
                text(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608 - constant name
            ).scalar_one_or_none()
            return revision
    finally:
        engine.dispose()


def is_up_to_date(db_path: Path) -> bool:
    """Whether ``db_path`` is migrated to the head revision.

    The check readiness needs (requirement 18.14): a database one revision behind the code reading
    it is a deployment error, and it presents as a missing column on whichever query happens to
    need it first rather than as a startup failure.
    """
    return current_revision(db_path) == head_revision()
