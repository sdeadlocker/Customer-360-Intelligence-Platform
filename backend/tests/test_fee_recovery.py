"""Fee recovery (Phase 22 play 6).

The point of these tests is that the seeded dataset contains *deliberately wrong billing*
(:mod:`c360.generator.fees` plants four defects on purpose), so the detector can be asserted to find
what was planted rather than merely asserted not to crash. A detector verified against clean data
has never been tested.

Four layers are covered:

* the fee schedule loads from the committed file and refuses to load a broken one;
* the generator actually emitted each planted defect;
* the scan finds each of the four leak types, and — as importantly — declines to raise a finding for
  a cycle that was legitimately waived or a waiver run inside the courtesy allowance;
* the endpoint enforces entitlement, masks recoverable amounts per role, and never writes.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from c360.core.config import Settings
from c360.data.repositories.fee_recovery import SqliteFeeRecoveryRepository
from c360.domain.fees import (
    FEE_CATEGORY,
    FEE_MERCHANT_MAINTENANCE,
    FEE_MERCHANT_WIRE,
    WIRE_EVENT_MERCHANT,
    FeeSchedule,
    FeeScheduleError,
    WaiverRule,
    default_schedule_path,
)
from c360.domain.money import Cents
from c360.main import create_app
from c360.services.fee_recovery import FeeRecoveryService, LeakType
from tests.conftest import make_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

# The seeded roles this module drives. RM sees balances in full; contact centre and marketing see
# them banded, which is what makes the masking assertion meaningful.
_RM = ("rm.taylor", "rm-dev-password")
_CONTACT = ("contact.jordan", "contact-dev-password")
_RISK = ("risk.riley", "risk-dev-password")


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def schedule() -> FeeSchedule:
    return FeeSchedule.from_file(default_schedule_path())


@pytest.fixture(scope="module")
def service(phase5_engine: Engine, schedule: FeeSchedule) -> FeeRecoveryService:
    return FeeRecoveryService(
        SqliteFeeRecoveryRepository(phase5_engine),
        schedule,
        courtesy_waiver_cycles=3,
        min_finding_cents=1_000,
        lookback_months=12,
    )


@pytest.fixture(scope="module")
def customer_ids(phase5_engine: Engine) -> tuple[str, ...]:
    with phase5_engine.connect() as connection:
        rows = connection.execute(text("SELECT customer_id FROM customer ORDER BY customer_id"))
        return tuple(str(row[0]) for row in rows)


@pytest.fixture(scope="module")
def api_settings(phase5_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    audit_dir = tmp_path_factory.mktemp("fee_audit")
    return make_settings(
        sqlite_db_path=str(phase5_db),
        sqlite_audit_db_path=str(audit_dir / "audit.db"),
    )


@pytest.fixture(scope="module")
def api(api_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(api_settings)) as client:
        yield client


def _headers(client: TestClient, credentials: tuple[str, str]) -> dict[str, str]:
    username, password = credentials
    body = client.post("/auth/token", json={"username": username, "password": password}).json()
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


def _path(customer_id: str) -> str:
    return f"/customers/{customer_id}/revenue/fee-recovery"


# ---------------------------------------------------------------- the schedule
class TestFeeSchedule:
    """The committed schedule mirrors the corpus and fails loudly when it does not."""

    def test_the_committed_schedule_matches_the_product_sheets(self, schedule: FeeSchedule) -> None:
        # Amounts transcribed from each product sheet's "Rates and Fees" table.
        expected = {
            "DDA-EVERYDAY": 800,
            "DDA-PREMIER": 3_000,
            "SAV-CORE": 500,
            "SAV-HIYIELD": 1_000,
            "MMA-SELECT": 1_200,
            "BUS-CHK": 2_000,
        }
        actual = {code: int(rule.maintenance_fee_cents) for code, rule in schedule.products.items()}
        assert actual == expected

    def test_every_rule_cites_a_document(self, schedule: FeeSchedule) -> None:
        # A finding names its source, so a rule without a doc_id could not be defended.
        for rule in schedule.products.values():
            assert rule.doc_id.startswith(("pc-", "pol-"))
            assert rule.rule_basis

    def test_a_product_without_a_maintenance_fee_has_no_rule(self, schedule: FeeSchedule) -> None:
        # Certificates carry no maintenance fee, so nothing about them is billable.
        assert schedule.rule_for("CD-12M") is None
        assert schedule.rule_for("CD-36M") is None
        assert schedule.rule_for(None) is None

    def test_only_business_checking_prices_a_wire(self, schedule: FeeSchedule) -> None:
        # Premier includes domestic wires at no charge, so an unbilled wire there is correct.
        assert schedule.products["BUS-CHK"].outgoing_wire_fee_cents == Cents(2_500)
        assert schedule.products["DDA-PREMIER"].outgoing_wire_fee_cents is None

    def test_a_malformed_schedule_raises_rather_than_loading_empty(self, tmp_path: Path) -> None:
        # The whole point: an empty schedule would report "no leakage", which reads as good news.
        broken = tmp_path / "broken.json"
        broken.write_text('{"products": {}}', encoding="utf-8")
        with pytest.raises(FeeScheduleError):
            FeeSchedule.from_file(broken)
        with pytest.raises(FeeScheduleError):
            FeeSchedule.from_file(tmp_path / "absent.json")

    def test_a_rule_without_a_doc_id_is_rejected(self) -> None:
        with pytest.raises(FeeScheduleError, match="doc_id"):
            FeeSchedule.from_mapping({"products": {"X": {"maintenance_fee_cents": 100}}})

    def test_any_single_condition_waives(self) -> None:
        rule = WaiverRule(min_balance_cents=Cents(1_000), min_monthly_credit_cents=Cents(500))
        assert rule.waives(
            balance_cents=Cents(1_000),
            monthly_credit_cents=Cents(0),
            relationship_balance_cents=Cents(0),
        )
        assert rule.waives(
            balance_cents=Cents(0),
            monthly_credit_cents=Cents(500),
            relationship_balance_cents=Cents(0),
        )
        assert not rule.waives(
            balance_cents=Cents(999),
            monthly_credit_cents=Cents(499),
            relationship_balance_cents=Cents(0),
        )

    def test_a_rule_with_no_conditions_never_waives(self) -> None:
        rule = WaiverRule()
        assert rule.is_unconditional_charge
        assert not rule.waives(
            balance_cents=Cents(10**9),
            monthly_credit_cents=Cents(10**9),
            relationship_balance_cents=Cents(10**9),
        )


# ---------------------------------------------------------------- the seeded defects
class TestSeededFeeData:
    """The generator emitted fees, and planted each defect the detector claims to find."""

    def test_maintenance_fees_were_posted(self, phase5_engine: Engine) -> None:
        assert (
            _scalar(
                phase5_engine,
                "SELECT COUNT(*) FROM txn WHERE transaction_category = :c AND merchant = :m"
                " AND amount_cents < 0",
                {"c": FEE_CATEGORY, "m": FEE_MERCHANT_MAINTENANCE},
            )
            > 0
        ), "the seeded dataset must contain fee postings or there is nothing to recover"

    def test_reversals_were_planted(self, phase5_engine: Engine) -> None:
        # A reversal is the same category and merchant with a positive amount.
        assert (
            _scalar(
                phase5_engine,
                "SELECT COUNT(*) FROM txn WHERE transaction_category = :c AND merchant = :m"
                " AND amount_cents > 0",
                {"c": FEE_CATEGORY, "m": FEE_MERCHANT_MAINTENANCE},
            )
            > 0
        )

    def test_legacy_pricing_was_planted(self, phase5_engine: Engine, schedule: FeeSchedule) -> None:
        """Some posting is below its own product's current schedule amount."""
        with phase5_engine.connect() as connection:
            rows = connection.execute(
                text("""
                    SELECT a.product_code, -t.amount_cents AS amount
                    FROM txn t JOIN account a ON a.account_id = t.account_id
                    WHERE t.transaction_category = :c AND t.merchant = :m AND t.amount_cents < 0
                    """),
                {"c": FEE_CATEGORY, "m": FEE_MERCHANT_MAINTENANCE},
            ).all()
        assert rows, "there must be fee postings to compare against the schedule"
        below = [
            row
            for row in rows
            if (rule := schedule.rule_for(str(row[0]))) is not None
            and int(row[1]) < int(rule.maintenance_fee_cents)
        ]
        assert below, "a grandfathered (below-schedule) posting must be planted"

    def test_unbilled_wires_were_planted(self, phase5_engine: Engine) -> None:
        events = _scalar(
            phase5_engine,
            "SELECT COUNT(*) FROM txn WHERE merchant = :m",
            {"m": WIRE_EVENT_MERCHANT},
        )
        billed = _scalar(
            phase5_engine,
            "SELECT COUNT(*) FROM txn WHERE merchant = :m",
            {"m": FEE_MERCHANT_WIRE},
        )
        assert events > billed > 0, "some wires must be billed and some must not"

    def test_certificates_are_never_charged_a_maintenance_fee(self, phase5_engine: Engine) -> None:
        # The schedule omits certificates, so the generator must not bill them.
        assert (
            _scalar(
                phase5_engine,
                """
                SELECT COUNT(*) FROM txn t JOIN account a ON a.account_id = t.account_id
                WHERE t.transaction_category = :c AND a.product_code IN ('CD-12M', 'CD-36M')
                """,
                {"c": FEE_CATEGORY},
            )
            == 0
        )


# ---------------------------------------------------------------- the scan
class TestFeeRecoveryScan:
    """The detector finds the planted defects, and only those."""

    @staticmethod
    def _all_findings(
        service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for customer_id in customer_ids:
            for finding in service.get_fee_recovery(customer_id).findings:
                out.append((customer_id, finding))
        return out

    def test_every_leak_type_is_found_across_the_book(
        self, service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> None:
        found = {finding.leak_type for _, finding in self._all_findings(service, customer_ids)}
        assert found == {
            LeakType.MISSED_MINIMUM,
            LeakType.LEGACY_PRICING,
            LeakType.FEE_WAIVED,
            LeakType.UNBILLED_SERVICE,
        }

    def test_every_finding_cites_a_document_and_prices_the_gap(
        self, service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> None:
        findings = self._all_findings(service, customer_ids)
        assert findings, "the seeded book must yield findings"
        for _, finding in findings:
            assert finding.doc_id
            assert finding.rule_basis
            assert finding.evidence
            assert int(finding.monthly_cents) > 0
            # Annualized is the run rate: twelve times the monthly gap, exactly.
            assert int(finding.annualized_cents) == int(finding.monthly_cents) * 12
            assert finding.cycles >= 1
            assert finding.status == "OPEN"

    def test_totals_are_the_sum_of_the_findings(
        self, service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> None:
        # Integer cents end to end, so the total is exact rather than nearly right.
        for customer_id in customer_ids:
            view = service.get_fee_recovery(customer_id)
            assert int(view.monthly_recoverable_cents) == sum(
                int(f.monthly_cents) for f in view.findings
            )
            assert int(view.annualized_recoverable_cents) == sum(
                int(f.annualized_cents) for f in view.findings
            )

    def test_findings_are_ordered_largest_first_and_stably(
        self, service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> None:
        for customer_id in customer_ids:
            first = service.get_fee_recovery(customer_id).findings
            second = service.get_fee_recovery(customer_id).findings
            amounts = [int(f.annualized_cents) for f in first]
            assert amounts == sorted(amounts, reverse=True)
            # A repeated scan must agree about order, because the UI and the export both render it.
            assert [f.finding_id for f in first] == [f.finding_id for f in second]

    def test_a_clean_customer_returns_an_empty_view_not_an_error(
        self, service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> None:
        clean = [
            customer_id
            for customer_id in customer_ids
            if not service.get_fee_recovery(customer_id).findings
        ]
        assert clean, "the seeded book should contain correctly-billed customers too"
        view = service.get_fee_recovery(clean[0])
        assert int(view.annualized_recoverable_cents) == 0
        assert view.findings == ()
        # "Nothing recoverable" must be a real answer, not indistinguishable from a missing
        # customer.
        assert view.as_of_date

    def test_a_waiver_run_inside_the_courtesy_allowance_is_not_a_finding(
        self, phase5_engine: Engine, schedule: FeeSchedule
    ) -> None:
        # Raising the allowance above the longest planted run must silence every waiver finding.
        forgiving = FeeRecoveryService(
            SqliteFeeRecoveryRepository(phase5_engine),
            schedule,
            courtesy_waiver_cycles=99,
            min_finding_cents=1_000,
            lookback_months=12,
        )
        with phase5_engine.connect() as connection:
            rows = connection.execute(text("SELECT customer_id FROM customer"))
            ids = [str(row[0]) for row in rows]
        waived = [
            finding
            for customer_id in ids
            for finding in forgiving.get_fee_recovery(customer_id).findings
            if finding.leak_type is LeakType.FEE_WAIVED
        ]
        assert waived == []

    def test_the_minimum_finding_floor_suppresses_noise(
        self, phase5_engine: Engine, schedule: FeeSchedule, customer_ids: tuple[str, ...]
    ) -> None:
        strict = FeeRecoveryService(
            SqliteFeeRecoveryRepository(phase5_engine),
            schedule,
            courtesy_waiver_cycles=3,
            min_finding_cents=10_000_000,
            lookback_months=12,
        )
        total = sum(
            len(strict.get_fee_recovery(customer_id).findings) for customer_id in customer_ids
        )
        assert total == 0

    def test_the_open_cycle_is_not_treated_as_unbilled(
        self, service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> None:
        """The as-of month has not been billed yet, so it cannot be missing a fee.

        Regression guard: including it manufactured one phantom unbilled cycle for every billable
        account on the book, which inflated the finding count roughly fivefold.
        """
        singles = [
            finding
            for customer_id in customer_ids
            for finding in service.get_fee_recovery(customer_id).findings
            if finding.leak_type is LeakType.MISSED_MINIMUM and finding.cycles == 1
        ]
        # A genuine one-cycle gap is possible, but it must not be the dominant shape of the results.
        missed = [
            finding
            for customer_id in customer_ids
            for finding in service.get_fee_recovery(customer_id).findings
            if finding.leak_type is LeakType.MISSED_MINIMUM
        ]
        assert len(singles) < len(missed)

    def test_the_scan_never_writes_to_the_customer_database(self, phase5_engine: Engine) -> None:
        # The engine the scan reads through is opened read-only; a write must be refused.
        with (
            pytest.raises(OperationalError, match="readonly database"),
            phase5_engine.connect() as connection,
        ):
            connection.execute(text("UPDATE account SET balance_cents = 0"))


# ---------------------------------------------------------------- the endpoint
class TestFeeRecoveryApi:
    """Entitlement, masking, and the contract."""

    #: Kept next to the assertions so a new revenue route is a deliberate contract change.
    _EXPECTED_PATH: ClassVar[str] = "/customers/{customer_id}/revenue/fee-recovery"

    def test_the_route_is_published_in_the_openapi_spec(self, api: TestClient) -> None:
        spec = api.get("/openapi.json").json()
        assert self._EXPECTED_PATH in spec["paths"]

    def test_an_unauthenticated_request_is_rejected(self, api: TestClient) -> None:
        assert api.get(_path("C-00001")).status_code == 401

    def test_an_entitled_role_sees_amounts_in_full(
        self, api: TestClient, customer_ids: tuple[str, ...]
    ) -> None:
        headers = _headers(api, _RISK)
        response = api.get(_path(customer_ids[0]), headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["masked_fields"] == []
        assert isinstance(body["data"]["annualized_recoverable_cents"], int)

    def test_a_banded_role_never_receives_the_raw_amount(
        self, api: TestClient, service: FeeRecoveryService, customer_ids: tuple[str, ...]
    ) -> None:
        # Pick a customer that actually has a finding, so there is a value worth masking.
        with_findings = next(
            customer_id
            for customer_id in customer_ids
            if service.get_fee_recovery(customer_id).findings
        )
        response = api.get(_path(with_findings), headers=_headers(api, _CONTACT))
        assert response.status_code == 200
        body = response.json()
        data = body["data"]
        assert body["meta"]["masked_fields"], "a banded role must report what was masked"
        # A band is a server-computed string; the unmasked integer must be absent from the payload.
        assert isinstance(data["annualized_recoverable_cents"], str)
        for finding in data["findings"]:
            assert isinstance(finding["annualized_cents"], str)
            assert isinstance(finding["monthly_cents"], str)

    def test_the_account_label_never_carries_a_full_account_number(
        self,
        api: TestClient,
        service: FeeRecoveryService,
        customer_ids: tuple[str, ...],
        phase5_engine: Engine,
    ) -> None:
        with_findings = next(
            customer_id
            for customer_id in customer_ids
            if service.get_fee_recovery(customer_id).findings
        )
        body = api.get(_path(with_findings), headers=_headers(api, _RISK)).json()
        with phase5_engine.connect() as connection:
            numbers = [
                str(row[0])
                for row in connection.execute(
                    text("SELECT account_number FROM account WHERE customer_id = :c"),
                    {"c": with_findings},
                )
            ]
        assert numbers, "the customer must hold accounts for this to prove anything"
        for finding in body["data"]["findings"]:
            label = finding["account_label"]
            # Partial by construction: the label is built from the last four, so the full number is
            # never in the payload and no masking rule has to remember to redact it.
            assert "****" in label
            assert all(number not in label for number in numbers)

    def test_a_customer_outside_the_book_is_denied(self, api: TestClient) -> None:
        # rm.taylor holds a restricted book, so an id outside it must not resolve.
        response = api.get(_path("C-99999"), headers=_headers(api, _RM))
        assert response.status_code in {403, 404}
        assert response.json()["error"]["code"] in {
            "ENTITLEMENT_DENIED",
            "CUSTOMER_NOT_FOUND",
        }

    def test_a_nonexistent_customer_is_not_found_for_a_full_access_role(
        self, api: TestClient
    ) -> None:
        response = api.get(_path("C-99999"), headers=_headers(api, _RISK))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"

    def test_the_response_carries_the_standard_envelope(
        self, api: TestClient, customer_ids: tuple[str, ...]
    ) -> None:
        body = api.get(_path(customer_ids[0]), headers=_headers(api, _RISK)).json()
        assert set(body) == {"data", "meta"}
        assert body["meta"]["correlation_id"]
        assert "as_of" in body["meta"]

    def test_the_scan_is_audited(
        self,
        api: TestClient,
        api_settings: Settings,
        service: FeeRecoveryService,
        customer_ids: tuple[str, ...],
    ) -> None:
        with_findings = next(
            customer_id
            for customer_id in customer_ids
            if service.get_fee_recovery(customer_id).findings
        )
        assert api.get(_path(with_findings), headers=_headers(api, _RISK)).status_code == 200
        # The writer drains on a background thread, so poll rather than assume it has landed.
        row = _wait_for_audit(api_settings.audit_db_path, "REVENUE_FEE_RECOVERY")
        assert row is not None, "a fee-recovery scan must leave an audit record"
        customer, fields = row
        assert customer == with_findings
        # Leak types are recorded; a monetary value never is (requirement 18.8's restraint).
        assert any(leak.value in str(fields) for leak in LeakType)
        assert "cents" not in str(fields)


# ---------------------------------------------------------------- helpers
def _scalar(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> int:
    with engine.connect() as connection:
        value = connection.execute(text(sql), params or {}).scalar()
    return int(value or 0)


def _wait_for_audit(
    path: Path, action: str, *, timeout: float = 3.0
) -> tuple[str | None, str | None] | None:
    """Poll the audit database for the newest record of ``action``.

    The audit writer batches on its own thread (design §7.4), so a read straight after the request
    races it. Mirrors ``_wait_for_rows`` in ``test_audit.py``.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT customer_id, fields_accessed FROM audit_log"
                " WHERE action = ? ORDER BY audit_id DESC LIMIT 1",
                (action,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            return (row[0], row[1])
        time.sleep(0.02)
    return None
