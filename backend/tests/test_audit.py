"""Task 4.6: the append-only audit subsystem, single writer, fail-closed."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from c360.api.audit import record_access
from c360.security.audit import (
    AuditOutcome,
    AuditRecord,
    AuditWriter,
    initialize_audit_db,
    now_iso,
)
from c360.security.entitlement import AllScope
from c360.security.errors import AuditUnavailableError
from c360.security.model import Principal, Role
from c360.security.policy import policy_for_role


def _record(**overrides: object) -> AuditRecord:
    base: dict[str, object] = {
        "occurred_at": now_iso(),
        "actor_user_id": "risk.riley",
        "actor_role": "RISK",
        "action": "customer.read",
        "outcome": AuditOutcome.ALLOWED,
        "correlation_id": "cid-1",
        "customer_id": "C-0001",
    }
    base.update(overrides)
    return AuditRecord(**base)  # type: ignore[arg-type]


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


@pytest.fixture
def writer(audit_path: Path) -> Iterator[AuditWriter]:
    w = AuditWriter(audit_path)
    w.start()
    try:
        yield w
    finally:
        w.stop()


def _wait_for_rows(path: Path, expected: int, *, timeout: float = 3.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        con = sqlite3.connect(path)
        try:
            count = int(con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
        finally:
            con.close()
        if count >= expected:
            return count
        time.sleep(0.02)
    return count


class TestSchemaAndImmutability:
    def test_update_is_rejected(self, audit_path: Path) -> None:
        initialize_audit_db(audit_path)
        con = sqlite3.connect(audit_path)
        con.execute(
            "INSERT INTO audit_log (occurred_at, actor_user_id, actor_role, action, outcome, "
            "correlation_id) VALUES (?,?,?,?,?,?)",
            (now_iso(), "u", "RM", "customer.read", "ALLOWED", "cid"),
        )
        con.commit()
        with pytest.raises(sqlite3.Error, match="append-only"):
            con.execute("UPDATE audit_log SET action='tampered'")
        con.close()

    def test_delete_is_rejected(self, audit_path: Path) -> None:
        initialize_audit_db(audit_path)
        con = sqlite3.connect(audit_path)
        con.execute(
            "INSERT INTO audit_log (occurred_at, actor_user_id, actor_role, action, outcome, "
            "correlation_id) VALUES (?,?,?,?,?,?)",
            (now_iso(), "u", "RM", "customer.read", "ALLOWED", "cid"),
        )
        con.commit()
        with pytest.raises(sqlite3.Error, match="append-only"):
            con.execute("DELETE FROM audit_log")
        con.close()

    def test_outcome_check_constraint(self, audit_path: Path) -> None:
        initialize_audit_db(audit_path)
        con = sqlite3.connect(audit_path)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO audit_log (occurred_at, actor_user_id, actor_role, action, outcome, "
                "correlation_id) VALUES (?,?,?,?,?,?)",
                (now_iso(), "u", "RM", "customer.read", "MAYBE", "cid"),
            )
        con.close()

    def test_json_columns_reject_non_json(self, audit_path: Path) -> None:
        initialize_audit_db(audit_path)
        con = sqlite3.connect(audit_path)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO audit_log (occurred_at, actor_user_id, actor_role, action, outcome, "
                "correlation_id, fields_accessed) VALUES (?,?,?,?,?,?,?)",
                (now_iso(), "u", "RM", "customer.read", "ALLOWED", "cid", "not json"),
            )
        con.close()


class TestWriter:
    def test_a_submitted_record_is_written(self, writer: AuditWriter, audit_path: Path) -> None:
        assert writer.submit(_record())
        assert _wait_for_rows(audit_path, 1) == 1

    def test_a_batch_of_records_all_land(self, writer: AuditWriter, audit_path: Path) -> None:
        for index in range(50):
            assert writer.submit(_record(correlation_id=f"cid-{index}"))
        assert _wait_for_rows(audit_path, 50) == 50

    def test_denied_access_is_recorded(self, writer: AuditWriter, audit_path: Path) -> None:
        assert writer.submit(_record(outcome=AuditOutcome.DENIED, action="customer.read.denied"))
        _wait_for_rows(audit_path, 1)
        con = sqlite3.connect(audit_path)
        outcome = con.execute("SELECT outcome FROM audit_log").fetchone()[0]
        con.close()
        assert outcome == "DENIED"

    def test_the_writer_is_alive_after_start(self, writer: AuditWriter) -> None:
        assert writer.is_alive()


class TestFailClosed:
    def test_submit_returns_false_when_the_queue_is_full(self, audit_path: Path) -> None:
        """A saturated queue is reported so the request can fail closed (design §7.4)."""
        w = AuditWriter(audit_path, max_queue=1)
        # Do not start the writer thread: nothing drains, so the queue saturates deterministically.
        assert w.submit(_record()) is True
        # Second submit fills or overflows the size-1 queue.
        results = [w.submit(_record()) for _ in range(5)]
        assert results.count(False) >= 1

    def test_record_access_raises_when_the_sink_fails_closed(self) -> None:
        class FullSink:
            def submit(self, _record: AuditRecord) -> bool:
                return False

        principal = Principal(
            user_id="risk.riley",
            role=Role.RISK,
            entitlement=AllScope(),
            field_policy=policy_for_role(Role.RISK),
            knowledge_levels=frozenset(),
        )
        with pytest.raises(AuditUnavailableError):
            record_access(
                FullSink(),
                principal,
                action="customer.read",
                outcome=AuditOutcome.ALLOWED,
                customer_id="C-0001",
            )


class TestRecordAccessHelper:
    def test_it_builds_and_submits_a_record_with_a_field_manifest(
        self, writer: AuditWriter, audit_path: Path
    ) -> None:
        principal = Principal(
            user_id="rm.taylor",
            role=Role.RM,
            entitlement=AllScope(),
            field_policy=policy_for_role(Role.RM),
            knowledge_levels=frozenset(),
        )
        record_access(
            writer,
            principal,
            action="customer.read",
            outcome=AuditOutcome.ALLOWED,
            customer_id="C-0001",
            fields_accessed=["customer_name", "date_of_birth"],
        )
        _wait_for_rows(audit_path, 1)
        con = sqlite3.connect(audit_path)
        role, fields = con.execute("SELECT actor_role, fields_accessed FROM audit_log").fetchone()
        con.close()
        assert role == "RM"
        assert "customer_name" in fields
