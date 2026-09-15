"""Knowledge endpoints and the ``knowledge_search`` tool (task 7.8).

These exercise the two integration surfaces the design §9.6 puts knowledge behind: the REST
endpoints (``/knowledge/search``, ``/knowledge/documents/{doc_id}``) and the typed tool registered
in the same registry as the customer tools. The load-bearing assertions are entitlement (a
Marketing role cannot retrieve or resolve a RISK_ONLY document, and its existence is not revealed)
and that a retrieval writes ``retrieved_doc_ids`` to the audit log (requirement 12.6).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from c360.api.readiness import REGISTRY
from c360.core.logging import configure_logging
from c360.knowledge.ingest import ingest_knowledge
from c360.main import create_app
from c360.security.model import Role, knowledge_levels_for_role
from c360.tools.knowledge_tool import KnowledgeSearchArgs
from c360.tools.registry import ToolContext, build_tool_registry
from tests.conftest import make_settings

_DIMENSIONS = 256


@pytest.fixture(scope="module")
def knowledge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("kb_api") / "knowledge.db"
    ingest_knowledge(database=path, dimensions=_DIMENSIONS)
    return path


@pytest.fixture
def kb_app(
    knowledge_db: Path, phase5_db: Path, tmp_path_factory: pytest.TempPathFactory
) -> FastAPI:
    REGISTRY.clear()
    audit_dir = tmp_path_factory.mktemp("kb_audit")
    settings = make_settings(
        sqlite_db_path=str(phase5_db),
        sqlite_knowledge_db_path=str(knowledge_db),
        sqlite_audit_db_path=str(audit_dir / "audit.db"),
        bedrock_embed_dimensions=str(_DIMENSIONS),
    )
    configure_logging(settings, force=True)
    return create_app(settings)


@pytest.fixture
def kb_client(kb_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(kb_app) as client:
        yield client


def _headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    body = client.post("/auth/token", json={"username": username, "password": password}).json()
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


_RM = ("rm.taylor", "rm-dev-password")
_RISK = ("risk.riley", "risk-dev-password")
_MARKETING = ("marketing.avery", "marketing-dev-password")


# ---------------------------------------------------------------- readiness


class TestReadiness:
    def test_knowledge_check_passes_when_ingested(self, kb_client: TestClient) -> None:
        body = kb_client.get("/ready").json()
        knowledge = next(c for c in body["data"]["components"] if c["name"] == "knowledge")
        assert knowledge["status"] == "pass"
        assert "chunks=" in knowledge["detail"]


# ---------------------------------------------------------------- search endpoint


class TestSearchEndpoint:
    def test_returns_cited_passages(self, kb_client: TestClient) -> None:
        response = kb_client.get(
            "/knowledge/search",
            params={"q": "overdraft policy"},
            headers=_headers(kb_client, *_RM),
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["passages"]
        for passage in data["passages"]:
            assert passage["doc_id"]
            assert passage["section_path"]
            assert passage["effective_from"]

    def test_requires_authentication(self, kb_client: TestClient) -> None:
        assert kb_client.get("/knowledge/search", params={"q": "x"}).status_code == 401

    def test_marketing_cannot_retrieve_risk_only(self, kb_client: TestClient) -> None:
        response = kb_client.get(
            "/knowledge/search",
            params={"q": "suspicious activity report fraud escalation"},
            headers=_headers(kb_client, *_MARKETING),
        )
        assert response.status_code == 200
        doc_ids = [p["doc_id"] for p in response.json()["data"]["passages"]]
        assert not any(doc_id.startswith("proc-") for doc_id in doc_ids)
        assert not any(doc_id.startswith("comp-") for doc_id in doc_ids)

    def test_risk_can_retrieve_risk_only(self, kb_client: TestClient) -> None:
        response = kb_client.get(
            "/knowledge/search",
            params={"q": "suspicious activity report fraud escalation"},
            headers=_headers(kb_client, *_RISK),
        )
        doc_ids = [p["doc_id"] for p in response.json()["data"]["passages"]]
        assert any(doc_id.startswith("proc-") for doc_id in doc_ids)

    def test_retrieval_is_audited(self, kb_client: TestClient, kb_app: FastAPI) -> None:
        kb_client.get(
            "/knowledge/search",
            params={"q": "overdraft policy fee"},
            headers=_headers(kb_client, *_RM),
        )
        # Drain the audit writer and read the record back.
        writer = kb_app.state.audit_writer
        writer.stop()
        rows = _audit_rows(kb_app.state.settings.audit_db_path)
        knowledge_writes = [r for r in rows if r["action"] == "KNOWLEDGE_SEARCH"]
        assert knowledge_writes
        assert knowledge_writes[-1]["retrieved_doc_ids"] is not None


# ---------------------------------------------------------------- document endpoint


class TestDocumentEndpoint:
    def test_returns_metadata(self, kb_client: TestClient) -> None:
        response = kb_client.get(
            "/knowledge/documents/pol-overdraft", headers=_headers(kb_client, *_RM)
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["doc_id"] == "pol-overdraft"
        assert data["version"] == "v2"  # latest by effective date

    def test_marketing_gets_404_for_risk_only_document(self, kb_client: TestClient) -> None:
        """Absent and forbidden collapse to 404 so the endpoint is not an existence oracle."""
        response = kb_client.get(
            "/knowledge/documents/proc-sar-filing", headers=_headers(kb_client, *_MARKETING)
        )
        assert response.status_code == 404

    def test_risk_can_read_risk_only_document(self, kb_client: TestClient) -> None:
        response = kb_client.get(
            "/knowledge/documents/proc-sar-filing", headers=_headers(kb_client, *_RISK)
        )
        assert response.status_code == 200
        assert response.json()["data"]["access_level"] == "RISK_ONLY"

    def test_unknown_document_is_404(self, kb_client: TestClient) -> None:
        response = kb_client.get(
            "/knowledge/documents/does-not-exist", headers=_headers(kb_client, *_RM)
        )
        assert response.status_code == 404


# ---------------------------------------------------------------- tool path


class TestKnowledgeTool:
    def test_tool_is_registered(self) -> None:
        registry = build_tool_registry()
        assert "knowledge_search" in registry.names()

    def test_tool_enforces_entitlement_like_the_endpoint(self, kb_app: FastAPI) -> None:
        """A Marketing principal calling the tool is filtered exactly as over REST (11.6)."""
        from c360.api.services import get_services  # noqa: PLC0415
        from c360.security.entitlement import scope_from_claim  # noqa: PLC0415
        from c360.security.model import Principal  # noqa: PLC0415
        from c360.security.policy import policy_for_role  # noqa: PLC0415

        # Build the services the same way a request would, off the app.
        class _Req:
            app = kb_app

        services = get_services(_Req())  # type: ignore[arg-type]
        assert services is not None
        assert services.knowledge is not None

        registry = build_tool_registry()
        marketing = Principal(
            user_id="marketing.avery",
            role=Role.MARKETING,
            entitlement=scope_from_claim("SEGMENT", ["MASS"]),
            field_policy=policy_for_role(Role.MARKETING),
            knowledge_levels=knowledge_levels_for_role(Role.MARKETING),
        )
        context = ToolContext(principal=marketing, services=services)
        result = registry.execute(
            "knowledge_search",
            context,
            KnowledgeSearchArgs(query="suspicious activity report fraud escalation"),
        )
        doc_ids = {p.doc_id for p in result.passages}  # type: ignore[attr-defined]
        assert not any(doc_id.startswith("proc-") for doc_id in doc_ids)


def _audit_rows(path: Path) -> list[dict[str, object]]:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM audit_log")]
    finally:
        connection.close()
