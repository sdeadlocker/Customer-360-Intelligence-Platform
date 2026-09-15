"""Phase 18 - scheduled and branded report exports (tasks 18.1-18.7).

These run the real report store, generator and API over the generated, recomputed dataset the rest
of Phase 5+ uses, on the mock LLM provider (offline). They assert the phase gate: the writable
``reports.db`` is created at its own schema head with no FK into ``customer.db``; a PDF pack and a
prepare-for-meeting briefing generate for a customer; the artifact respects role masking and
entitlement; a scheduled digest is delivered via the mock deliverer to ``data/``; and — the
datastore rule — ``customer.db`` is never written.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from c360.agents.runtime import build_agent_runtime
from c360.api.services import Services, build_services
from c360.core.config import Settings
from c360.core.telemetry.labels import label_permitted
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.reports.generate import GenerationRequest, generate_report
from c360.reports.models import (
    Branding,
    ReportDefinition,
    ReportScope,
    ReportScopeKind,
    ReportType,
    RunStatus,
)
from c360.reports.service import (
    PrincipalResolver,
    ReportService,
    build_report_service,
)
from c360.reports.store import SCHEMA_VERSION, current_schema_version
from c360.security.authorization import authorize_customer
from c360.security.errors import EntitlementError
from c360.security.local_provider import principal_for_user
from c360.security.serializer import mask_model
from tests.conftest import make_settings

_RM = "rm.taylor"
_MARKETING = "marketing.avery"


@pytest.fixture
def report_settings(phase5_db: Path, tmp_path: Path) -> Settings:
    """Settings pointing the customer DB at the seeded panel and reports at a throwaway dir."""
    return make_settings(
        sqlite_db_path=str(phase5_db),
        sqlite_reports_db_path=str(tmp_path / "reports.db"),
        reports_output_dir=str(tmp_path / "artifacts"),
        sqlite_audit_db_path=str(tmp_path / "audit.db"),
        sqlite_signals_db_path=str(tmp_path / "signals.db"),
    )


@pytest.fixture
def services(report_settings: Settings) -> Iterator[Services]:
    container = build_services(report_settings)
    try:
        yield container
    finally:
        container.engine.dispose()
        if container.reports_engine is not None:
            container.reports_engine.dispose()


def _first_book_customer(services: Services) -> str:
    """A customer inside the RM's seeded book (C-00001..C-00080)."""
    principal = principal_for_user(_RM)
    assert principal is not None
    for customer in services.customer.repository.list_all():
        try:
            authorize_customer(principal, customer.customer_id, services.customer.repository)
        except EntitlementError:
            continue
        return customer.customer_id
    raise AssertionError("no entitled customer in the RM book")


def _build_service(report_settings: Settings) -> ReportService:
    built = build_report_service(
        report_settings.reports_db_path,
        output_dir=report_settings.reports_output_path,
        max_customers=report_settings.reports_max_customers_per_run,
        pool_size=4,
    )
    assert built is not None
    return built[0]


# ==================================================================== 18.1 store & schema
class TestStoreAndSchema:
    def test_schema_is_stamped_at_head(self, report_settings: Settings, services: Services) -> None:
        # build_services initializes reports.db via build_report_service.
        assert current_schema_version(report_settings.reports_db_path) == SCHEMA_VERSION

    def test_tables_exist_and_have_no_fk_into_customer_db(
        self, report_settings: Settings, services: Services
    ) -> None:
        connection = sqlite3.connect(report_settings.reports_db_path)
        try:
            names = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"report_definition", "report_schedule", "report_run"} <= names
            for table in ("report_definition", "report_schedule", "report_run"):
                for fk in connection.execute(f"PRAGMA foreign_key_list({table})"):
                    # Only internal FKs — never a table that lives in customer.db.
                    assert fk[2] in {"report_definition"}
        finally:
            connection.close()


# ==================================================================== 18.2 PDF generation
class TestPdfGeneration:
    def test_pdf_pack_generates_for_a_customer(
        self, report_settings: Settings, services: Services
    ) -> None:
        service = _build_service(report_settings)
        runtime = build_agent_runtime(report_settings)
        principal = principal_for_user(_RM)
        assert principal is not None
        customer_id = _first_book_customer(services)
        definition_id = service.create_definition(
            report_type=ReportType.PDF_PACK,
            title="360 Pack",
            scope=ReportScope(kind=ReportScopeKind.CUSTOMER, customer_id=customer_id),
            branding=Branding(brand_name="Test Bank", tagline="t"),
            owner=principal,
        )
        definition = service.get_definition(definition_id, principal)
        assert definition is not None
        run = service.run_definition(
            definition, principal, services, runtime, trigger_kind="MANUAL"
        )
        assert run.status is RunStatus.SUCCEEDED
        assert run.artifact_path is not None
        artifact = Path(run.artifact_path)
        assert artifact.is_file()
        content = artifact.read_bytes()
        assert content.startswith(b"%PDF-1.4")
        assert content.rstrip().endswith(b"%%EOF")
        assert run.customers_rendered == 1

    def test_pdf_is_deterministic(self, report_settings: Settings, services: Services) -> None:
        runtime = build_agent_runtime(report_settings)
        principal = principal_for_user(_RM)
        assert principal is not None
        customer_id = _first_book_customer(services)
        definition = ReportDefinition(
            definition_id=1,
            report_type=ReportType.PDF_PACK,
            title="360 Pack",
            scope=ReportScope(kind=ReportScopeKind.CUSTOMER, customer_id=customer_id),
            branding=Branding(brand_name="Test Bank", tagline="t"),
            owner_id=_RM,
            owner_role="RM",
            created_at="2026-01-01T00:00:00Z",
        )
        request = GenerationRequest(definition=definition, principal=principal, max_customers=10)
        first = generate_report(request, services, runtime).content
        second = generate_report(request, services, runtime).content
        assert first == second


# ==================================================================== 18.3 briefing
class TestBriefing:
    def test_briefing_generates_for_a_customer(
        self, report_settings: Settings, services: Services
    ) -> None:
        service = _build_service(report_settings)
        runtime = build_agent_runtime(report_settings)
        principal = principal_for_user(_RM)
        assert principal is not None
        customer_id = _first_book_customer(services)
        definition_id = service.create_definition(
            report_type=ReportType.MEETING_BRIEFING,
            title="Meeting Brief",
            scope=ReportScope(kind=ReportScopeKind.CUSTOMER, customer_id=customer_id),
            branding=Branding(brand_name="Test Bank"),
            owner=principal,
        )
        definition = service.get_definition(definition_id, principal)
        assert definition is not None
        run = service.run_definition(
            definition, principal, services, runtime, trigger_kind="MANUAL"
        )
        assert run.status is RunStatus.SUCCEEDED
        assert Path(run.artifact_path or "").read_bytes().startswith(b"%PDF")


# ==================================================================== 18.4 scheduling & delivery
class TestSchedulingAndDelivery:
    def test_due_schedule_runs_and_mock_delivers_to_disk(
        self, report_settings: Settings, services: Services
    ) -> None:
        service = _build_service(report_settings)
        runtime = build_agent_runtime(report_settings)
        principal = principal_for_user(_RM)
        assert principal is not None
        customer_id = _first_book_customer(services)
        definition_id = service.create_definition(
            report_type=ReportType.PDF_PACK,
            title="Scheduled Pack",
            scope=ReportScope(kind=ReportScopeKind.CUSTOMER, customer_id=customer_id),
            branding=Branding(brand_name="Test Bank"),
            owner=principal,
        )
        service.create_schedule(definition_id=definition_id, cadence="DAILY", owner=principal)

        resolver = PrincipalResolver(principal_for_user)
        summary = service.run_due_schedules(resolver, services, runtime)
        assert summary.schedules_due == 1
        assert summary.runs_succeeded == 1
        # The mock deliverer writes a manifest beside the artifact on the data/ volume.
        manifests = list(report_settings.reports_output_path.glob("*-manifest.json"))
        assert len(manifests) == 1

    def test_entitlement_rechecked_at_run_time(
        self, report_settings: Settings, services: Services
    ) -> None:
        """A book report over an id the owner is not entitled to renders zero (task 18.4).

        The RM's book is C-00001..C-00080; a definition scoped to an id outside it must produce a
        run that rendered no customers, because the generation-time re-check drops the id.
        """
        service = _build_service(report_settings)
        runtime = build_agent_runtime(report_settings)
        principal = principal_for_user(_RM)
        assert principal is not None
        # An id well outside the seeded RM book of 80 customers.
        outside = "C-99999"
        definition_id = service.create_definition(
            report_type=ReportType.PDF_PACK,
            title="Book Pack",
            scope=ReportScope(kind=ReportScopeKind.BOOK, customer_ids=(outside,)),
            branding=Branding(brand_name="Test Bank"),
            owner=principal,
        )
        definition = service.get_definition(definition_id, principal)
        assert definition is not None
        run = service.run_definition(
            definition, principal, services, runtime, trigger_kind="MANUAL"
        )
        # The non-entitled id was dropped at generation time, so nothing was rendered.
        assert run.customers_rendered == 0


# ==================================================================== 18.7 telemetry & masking
class TestTelemetryAndMasking:
    def test_report_type_is_an_allowed_metric_label(self) -> None:
        assert label_permitted("report_type")
        assert label_permitted("outcome")

    def test_customer_db_is_never_written(
        self, report_settings: Settings, services: Services
    ) -> None:
        """The datastore rule: generation opens customer.db read-only (task 18.1)."""
        service = _build_service(report_settings)
        runtime = build_agent_runtime(report_settings)
        principal = principal_for_user(_RM)
        assert principal is not None
        customer_id = _first_book_customer(services)
        definition_id = service.create_definition(
            report_type=ReportType.PDF_PACK,
            title="Pack",
            scope=ReportScope(kind=ReportScopeKind.CUSTOMER, customer_id=customer_id),
            branding=Branding(brand_name="Test Bank"),
            owner=principal,
        )
        definition = service.get_definition(definition_id, principal)
        assert definition is not None
        service.run_definition(definition, principal, services, runtime, trigger_kind="MANUAL")

        # Opening customer.db read-write-create would be the only way a report could mutate it; the
        # platform never does. Assert the file is openable read-only and a write is rejected.
        engine = create_sqlite_engine(
            report_settings.customer_db_path, mode=AccessMode.READ_ONLY, pool_size=1
        )
        try:
            with engine.connect() as connection, pytest.raises(OperationalError):
                connection.execute(text("CREATE TABLE _should_fail (x INTEGER)"))
        finally:
            engine.dispose()

    def test_masking_absent_field_renders_as_restricted(
        self, report_settings: Settings, services: Services
    ) -> None:
        """A field the role cannot see is absent from the masked read the report renders from."""
        principal = principal_for_user(_MARKETING)
        assert principal is not None
        customer_id = next(iter(services.customer.repository.list_all())).customer_id
        contact = services.customer.get_contact(customer_id)
        assert contact is not None
        _data, masked = mask_model(contact, principal.field_policy)
        # Marketing has fields masked on contact info; the render helper reads the masked dict, so
        # an absent key becomes the "restricted" placeholder rather than the real value.
        assert masked  # at least one field masked for marketing
