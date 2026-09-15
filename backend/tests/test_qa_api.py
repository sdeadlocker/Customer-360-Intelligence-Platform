"""Phase 9 — the ``POST /customers/{id}/ask`` SSE endpoint (task 9.5, design §6.3, §10).

The eleventh success criterion: natural-language questions return grounded answers with supporting
detail, handle policy questions from the ingested corpus, refuse correctly, and never leak across
customers. These run the real app with the mock provider over the seeded dataset and an ingested
knowledge base, so retrieval and grounding are exercised end to end without AWS.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from c360.api.readiness import REGISTRY
from c360.core.logging import configure_logging
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.knowledge.ingest import ingest_knowledge
from c360.main import create_app
from tests.conftest import make_settings
from tests.phase5_fixtures import query_all

_DIMENSIONS = 256
# risk.riley carries an ALL entitlement scope, so it is entitled to every seeded customer id (the
# RM's BOOK uses a different id format than the generator's, exactly as the Phase 8 SSE tests note).
_RISK = ("risk.riley", "risk-dev-password")
_MARKETING = ("marketing.avery", "marketing-dev-password")


@pytest.fixture(scope="module")
def knowledge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("qa_kb") / "knowledge.db"
    ingest_knowledge(database=path, dimensions=_DIMENSIONS)
    return path


@pytest.fixture
def app(knowledge_db: Path, phase5_db: Path, tmp_path_factory: pytest.TempPathFactory) -> FastAPI:
    REGISTRY.clear()
    work = tmp_path_factory.mktemp("qa_api")
    settings = make_settings(
        sqlite_db_path=str(phase5_db),
        sqlite_knowledge_db_path=str(knowledge_db),
        sqlite_audit_db_path=str(work / "audit.db"),
        sqlite_checkpoint_db_path=str(work / "checkpoints.db"),
        bedrock_embed_dimensions=str(_DIMENSIONS),
    )
    configure_logging(settings, force=True)
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _headers(client: TestClient, creds: tuple[str, str]) -> dict[str, str]:
    body = client.post("/auth/token", json={"username": creds[0], "password": creds[1]}).json()
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


def _customer_id(phase5_db: Path) -> str:
    engine = create_sqlite_engine(phase5_db, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        return str(query_all(engine, "SELECT customer_id FROM customer ORDER BY customer_id")[0][0])
    finally:
        engine.dispose()


def _parse_sse(text: str) -> list[tuple[str, dict]]:
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


def _ask(
    client: TestClient, cid: str, question: str, *, session_id: str = "s1", creds=_RISK
) -> Response:
    return client.post(
        f"/customers/{cid}/ask",
        json={"question": question, "session_id": session_id},
        headers=_headers(client, creds),
    )


def _first_customer_and_name(phase5_db: Path) -> tuple[str, str]:
    engine = create_sqlite_engine(phase5_db, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        row = query_all(
            engine,
            "SELECT customer_id, customer_name FROM customer ORDER BY customer_id LIMIT 1",
        )[0]
        return str(row[0]), str(row[1])
    finally:
        engine.dispose()


def _ask_all(client: TestClient, question: str, *, session_id: str = "sx", creds=_RISK) -> Response:
    return client.post(
        "/ask",
        json={"question": question, "session_id": session_id},
        headers=_headers(client, creds),
    )


# ---------------------------------------------------------------- grounded fact answer


def test_fact_question_streams_grounded_answer_with_citations(client: TestClient, phase5_db: Path):
    cid = _customer_id(phase5_db)
    response = _ask(client, cid, "what is their net worth?")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(response.text)
    kinds = [event for event, _ in events]
    assert "token" in kinds
    assert kinds[-1] == "done"

    answer = "".join(data["text"] for event, data in events if event == "token")
    assert answer.strip(), "the answer must not be empty"
    assert "[F" in answer, "a grounded fact answer cites facts by [F] id"

    citations = next(data for event, data in events if event == "citations")
    assert citations["fact_citations"], "fact citations returned separately (design §9.1)"
    assert isinstance(citations["passage_citations"], list)


# ---------------------------------------------------------------- policy question from the corpus


def test_policy_question_answers_from_the_knowledge_corpus(client: TestClient, phase5_db: Path):
    cid = _customer_id(phase5_db)
    response = _ask(client, cid, "what are the eligibility requirements for a HELOC?")
    assert response.status_code == 200
    events = _parse_sse(response.text)
    done = next(data for event, data in events if event == "done")
    citations = next(data for event, data in events if event == "citations")
    # With an ingested corpus a policy question should retrieve passages, cited separately from
    # facts; and it should not be flagged "no guidance found".
    assert citations["passage_citations"], "a corpus policy question returns passage citations"
    assert done["no_guidance"] is False


# ---------------------------------------------------------------- auth and existence


def test_ask_requires_authentication(client: TestClient, phase5_db: Path):
    cid = _customer_id(phase5_db)
    response = client.post(f"/customers/{cid}/ask", json={"question": "x", "session_id": "s1"})
    assert response.status_code == 401


def test_ask_for_unknown_customer_is_404(client: TestClient):
    response = _ask(client, "C-NOPE", "what is their net worth?")
    assert response.status_code == 404


# ---------------------------------------------------------------- no cross-customer leakage


def test_no_cross_customer_leakage_on_switch(client: TestClient, phase5_db: Path):
    engine = create_sqlite_engine(phase5_db, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        ids = [
            str(r[0])
            for r in query_all(engine, "SELECT customer_id FROM customer ORDER BY customer_id")
        ]
    finally:
        engine.dispose()
    c1, c2 = ids[0], ids[1]
    headers = _headers(client, _RISK)

    def ask(cid: str, q: str) -> Response:
        return client.post(
            f"/customers/{cid}/ask", json={"question": q, "session_id": "shared"}, headers=headers
        )

    first = ask(c1, "what is their net worth?")
    assert first.status_code == 200
    # Switch to c2 in the same session; the answer must be about c2's record, never c1's.
    second = ask(c2, "what is their net worth?")
    assert second.status_code == 200
    citations = next(data for event, data in _parse_sse(second.text) if event == "citations")
    for citation in citations["fact_citations"]:
        assert citation["entity_id"] == c2, "a switched turn must not cite the prior customer"


# ---------------------------------------------------------------- cross-customer /ask (task 9.6)


def test_cross_customer_ask_resolves_the_customer_and_answers(client: TestClient, phase5_db: Path):
    """The search-landing Ask AI: no customer in the path, resolved from the question itself."""
    cid, name = _first_customer_and_name(phase5_db)
    response = _ask_all(client, f"what is the net worth of {name}?")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(response.text)
    kinds = [event for event, _ in events]
    assert "token" in kinds
    assert kinds[-1] == "done"

    answer = "".join(data["text"] for event, data in events if event == "token")
    assert answer.strip(), "the cross-customer answer must not be empty"

    # The agent resolved the named customer and grounded the answer in their facts.
    citations = next(data for event, data in events if event == "citations")
    assert citations["fact_citations"], "a resolved cross-customer answer cites facts"
    for citation in citations["fact_citations"]:
        assert citation["entity_id"] == cid, "the answer is grounded in the resolved customer"


def test_cross_customer_ask_requires_authentication(client: TestClient):
    response = client.post("/ask", json={"question": "who is anyone?", "session_id": "s1"})
    assert response.status_code == 401


def test_cross_customer_ask_resolves_no_one_for_an_unknown_name(client: TestClient):
    """A question naming no seeded customer resolves nobody and cites no facts (requirement 11.5).

    The resolver returns no match, so the agent has no customer to read and grounds nothing — the
    honest "no matching customer" outcome, never a fabricated answer about someone who is not there.
    """
    response = _ask_all(client, "what is the net worth of Zzyzx Nonexistent Person?")
    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [event for event, _ in events][-1] == "done"
    citations = next(data for event, data in events if event == "citations")
    assert citations["fact_citations"] == [], "no customer resolved, so no fact is cited"
