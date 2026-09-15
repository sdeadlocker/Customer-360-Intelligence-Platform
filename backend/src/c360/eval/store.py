"""The evaluation datastore -- ``eval.db`` (task 11.2, design §14.2, §14.6).

Every evaluation run is recorded here so a quality change is always attributable (design §14.6): the
run header captures the prompt version, model id, provider, data seed, code revision and a JSON
snapshot of the config that produced it. The scores are stored per *dimension* (the 16 dimensions of
design §14.3) with the numeric value, the gate threshold, and whether the dimension passed, so a
report or a champion/challenger diff reads structured rows rather than re-parsing a narrative.

Following the platform's auxiliary-database convention, ``eval.db`` is a raw-schema SQLite file
created by :func:`initialize_eval_db` (mirroring :func:`c360.security.audit.initialize_audit_db`),
not an Alembic-migrated database -- it is derived, rebuildable output, never a system of record.
Unlike
the audit log it is *not* append-only: a run can be re-scored and the baseline is a mutable pointer
to the current champion.

The store speaks in the value types of :mod:`c360.eval.results` (``RunRecord``, ``DimensionScore``),
so a caller never assembles SQL. Reads return those same types, which is what lets the reporter and
the comparator work against a run without knowing it came from a database.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Final

from c360.eval.results import DimensionScore, RunRecord

# ---------------------------------------------------------------- schema
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS eval_run (
  run_id          TEXT PRIMARY KEY,
  created_at      TEXT NOT NULL,
  mode            TEXT NOT NULL,
  provider        TEXT NOT NULL,
  model_id        TEXT NOT NULL,
  prompt_versions TEXT NOT NULL CHECK (json_valid(prompt_versions)),
  data_seed       INTEGER NOT NULL,
  data_count      INTEGER NOT NULL,
  code_revision   TEXT NOT NULL,
  config          TEXT NOT NULL CHECK (json_valid(config)),
  panel_size      INTEGER NOT NULL,
  passed          INTEGER NOT NULL CHECK (passed IN (0, 1))
);

CREATE TABLE IF NOT EXISTS eval_dimension_score (
  run_id      TEXT NOT NULL REFERENCES eval_run(run_id) ON DELETE CASCADE,
  dimension   INTEGER NOT NULL,
  name        TEXT NOT NULL,
  value       REAL,
  threshold   REAL,
  hard_gate   INTEGER NOT NULL CHECK (hard_gate IN (0, 1)),
  passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
  detail      TEXT NOT NULL CHECK (json_valid(detail)),
  PRIMARY KEY (run_id, dimension)
);

-- A single-row pointer at the champion run whose scores the next run is diffed against. Mutable:
-- `promote` overwrites it. Row is keyed on a constant so there is exactly one baseline.
CREATE TABLE IF NOT EXISTS eval_baseline (
  singleton   INTEGER PRIMARY KEY CHECK (singleton = 0),
  run_id      TEXT NOT NULL REFERENCES eval_run(run_id)
);
"""

_INSERT_RUN: Final = """
INSERT OR REPLACE INTO eval_run (
  run_id, created_at, mode, provider, model_id, prompt_versions, data_seed, data_count,
  code_revision, config, panel_size, passed
) VALUES (
  :run_id, :created_at, :mode, :provider, :model_id, :prompt_versions, :data_seed, :data_count,
  :code_revision, :config, :panel_size, :passed
)
"""

_INSERT_SCORE: Final = """
INSERT OR REPLACE INTO eval_dimension_score (
  run_id, dimension, name, value, threshold, hard_gate, passed, detail
) VALUES (
  :run_id, :dimension, :name, :value, :threshold, :hard_gate, :passed, :detail
)
"""


def initialize_eval_db(path: Path) -> None:
    """Create ``eval.db`` and its tables if they do not already exist (task 11.2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    try:
        connection.executescript(_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class EvalStore:
    """Read/write access to ``eval.db`` in the value types of :mod:`c360.eval.results`."""

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path
        initialize_eval_db(path)

    # ---------------------------------------------------------------- writes
    def save_run(self, record: RunRecord) -> None:
        """Persist a run header and all its dimension scores in one transaction."""
        connection = _connect(self._path)
        try:
            with connection:
                connection.execute(_INSERT_RUN, _run_params(record))
                connection.executemany(
                    _INSERT_SCORE,
                    [_score_params(record.run_id, score) for score in record.scores],
                )
        finally:
            connection.close()

    def set_baseline(self, run_id: str) -> None:
        """Point the baseline at ``run_id`` (promotion). Overwrites any existing baseline."""
        connection = _connect(self._path)
        try:
            with connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM eval_run WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    is None
                ):
                    raise KeyError(f"cannot set baseline to unknown run {run_id!r}")
                connection.execute(
                    "INSERT OR REPLACE INTO eval_baseline (singleton, run_id) VALUES (0, ?)",
                    (run_id,),
                )
        finally:
            connection.close()

    # ---------------------------------------------------------------- reads
    def get_run(self, run_id: str) -> RunRecord | None:
        """The run with ``run_id``, or ``None`` if it does not exist."""
        connection = _connect(self._path)
        try:
            connection.row_factory = sqlite3.Row
            header = connection.execute(
                "SELECT * FROM eval_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if header is None:
                return None
            scores = connection.execute(
                "SELECT * FROM eval_dimension_score WHERE run_id = ? ORDER BY dimension",
                (run_id,),
            ).fetchall()
            return _row_to_record(header, scores)
        finally:
            connection.close()

    def latest_run(self) -> RunRecord | None:
        """The most recently created run, or ``None`` when the store is empty."""
        connection = _connect(self._path)
        try:
            row = connection.execute(
                "SELECT run_id FROM eval_run ORDER BY created_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        return self.get_run(row[0]) if row is not None else None

    def baseline_run(self) -> RunRecord | None:
        """The current champion run the baseline points at, or ``None`` if none is set."""
        connection = _connect(self._path)
        try:
            row = connection.execute(
                "SELECT run_id FROM eval_baseline WHERE singleton = 0"
            ).fetchone()
        finally:
            connection.close()
        return self.get_run(row[0]) if row is not None else None


# ---------------------------------------------------------------- row mapping


def _run_params(record: RunRecord) -> dict[str, object]:
    return {
        "run_id": record.run_id,
        "created_at": record.created_at,
        "mode": record.mode,
        "provider": record.provider,
        "model_id": record.model_id,
        "prompt_versions": json.dumps(record.prompt_versions, sort_keys=True),
        "data_seed": record.data_seed,
        "data_count": record.data_count,
        "code_revision": record.code_revision,
        "config": json.dumps(record.config, sort_keys=True),
        "panel_size": record.panel_size,
        "passed": int(record.passed),
    }


def _score_params(run_id: str, score: DimensionScore) -> dict[str, object]:
    return {
        "run_id": run_id,
        "dimension": score.dimension,
        "name": score.name,
        "value": score.value,
        "threshold": score.threshold,
        "hard_gate": int(score.hard_gate),
        "passed": int(score.passed),
        "detail": json.dumps(score.detail, sort_keys=True),
    }


def _row_to_record(header: sqlite3.Row, scores: list[sqlite3.Row]) -> RunRecord:
    return RunRecord(
        run_id=header["run_id"],
        created_at=header["created_at"],
        mode=header["mode"],
        provider=header["provider"],
        model_id=header["model_id"],
        prompt_versions=json.loads(header["prompt_versions"]),
        data_seed=header["data_seed"],
        data_count=header["data_count"],
        code_revision=header["code_revision"],
        config=json.loads(header["config"]),
        panel_size=header["panel_size"],
        passed=bool(header["passed"]),
        scores=tuple(
            DimensionScore(
                dimension=row["dimension"],
                name=row["name"],
                value=row["value"],
                threshold=row["threshold"],
                hard_gate=bool(row["hard_gate"]),
                passed=bool(row["passed"]),
                detail=json.loads(row["detail"]),
            )
            for row in scores
        ),
    )


__all__ = ["EvalStore", "initialize_eval_db"]
