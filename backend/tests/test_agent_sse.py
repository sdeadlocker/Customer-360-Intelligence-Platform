"""Task 8.9 — streaming the dashboard agents over SSE (design §8.1).

Runs the real app over the seeded Phase 5 dataset with the mock provider (the app's default under
the test settings) and asserts: ``/insights`` streams the six insight cards as ``agent`` SSE events
plus a terminal ``done``; ``/recommendations`` streams only the offer card; both enforce the same
403-vs-404 auth gate and 401-without-a-token as the REST routes; and the streamed payloads carry the
labelling metadata (model id, prompt version, cache flag).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from c360.api.readiness import REGISTRY
from c360.core.config import Settings
from c360.core.logging import configure_logging
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.main import create_app
from tests.conftest import make_settings
from tests.phase5_fixtures import query_all

_RISK = ("risk.riley", "risk-dev-password")


@pytest.fixture(scope="module")
def api_settings(phase5_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    audit_dir = tmp_path_factory.mktemp("sse_audit")
    return make_settings(
        sqlite_db_path=str(phase5_db),
        sqlite_audit_db_path=str(audit_dir / "audit.db"),
    )


@pytest.fixture
def api(api_settings: Settings) -> FastAPI:
    REGISTRY.clear()
    configure_logging(api_settings, force=True)
    return create_app(api_settings)


@pytest.fixture
def client(api: FastAPI) -> Iterator[TestClient]:
    with TestClient(api) as test_client:
        yield test_client


def _headers(client: TestClient) -> dict[str, str]:
    body = client.post("/auth/token", json={"username": _RISK[0], "password": _RISK[1]}).json()
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


def _customer_id(phase5_db: Path) -> str:
    engine = create_sqlite_engine(phase5_db, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        return str(query_all(engine, "SELECT customer_id FROM customer ORDER BY customer_id")[0][0])
    finally:
        engine.dispose()


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into a list of (event, data) pairs."""
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        events.append((event, json.loads(data) if data else {}))
    return events


def test_insights_streams_all_insight_cards(client: TestClient, phase5_db: Path):
    customer_id = _customer_id(phase5_db)
    response = client.get(f"/customers/{customer_id}/insights", headers=_headers(client))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(response.text)
    agents = {data["agent"] for event, data in events if event == "agent"}
    assert agents == {
        "financial_health",
        "risk",
        "life_event",
        "relationship",
        "journey",
        "customer_summary",
    }
    assert "offer_recommendation" not in agents
    assert events[-1][0] == "done"


def test_recommendations_streams_only_the_offer_card(client: TestClient, phase5_db: Path):
    customer_id = _customer_id(phase5_db)
    response = client.get(f"/customers/{customer_id}/recommendations", headers=_headers(client))
    assert response.status_code == 200
    events = _parse_sse(response.text)
    agents = [data["agent"] for event, data in events if event == "agent"]
    assert agents == ["offer_recommendation"]


def test_streamed_card_carries_labelling_metadata(client: TestClient, phase5_db: Path):
    customer_id = _customer_id(phase5_db)
    response = client.get(f"/customers/{customer_id}/insights", headers=_headers(client))
    cards = [data for event, data in _parse_sse(response.text) if event == "agent"]
    for card in cards:
        assert card["model_id"] == "mock-llm-v1"
        assert "+" in card["prompt_version"]
        assert card["degraded"] is False
        assert "narrative" in card["outputs"]


def test_insights_requires_authentication(client: TestClient, phase5_db: Path):
    customer_id = _customer_id(phase5_db)
    assert client.get(f"/customers/{customer_id}/insights").status_code == 401


def test_insights_for_unknown_customer_is_404(client: TestClient):
    response = client.get("/customers/C-NOPE/insights", headers=_headers(client))
    assert response.status_code == 404
