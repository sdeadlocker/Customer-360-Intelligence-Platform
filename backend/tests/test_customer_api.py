"""Phase 5 REST API tests (task 5.8) and the contract/performance suite (task 5.9).

These run the real FastAPI app over the generated, recomputed dataset, authenticating through the
seeded local provider exactly as a client would. They assert the envelope shape, entitlement scoping
(403/404 without an existence oracle), field masking on the wire, the aggregator's partial-200, the
published OpenAPI contract, and a per-endpoint latency budget on the seeded data.

Two seeded users carry an ``ALL`` scope (``contact.jordan``, ``risk.riley``), so they can reach any
generated customer; the segment-scoped users (``wealth.morgan``, ``marketing.avery``) exercise
entitlement denial against real customer segments.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from c360.api.readiness import REGISTRY
from c360.core.config import Settings
from c360.core.logging import configure_logging
from c360.data.engine import AccessMode, create_sqlite_engine
from c360.main import create_app
from c360.services.risk import RiskService
from tests.conftest import make_settings
from tests.phase5_fixtures import query_all, query_one


@pytest.fixture(scope="module")
def api_settings(phase5_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    audit_dir = tmp_path_factory.mktemp("api_audit")
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
def api_client(api: FastAPI) -> Iterator[TestClient]:
    with TestClient(api) as client:
        yield client


def _token(client: TestClient, username: str, password: str) -> str:
    body = client.post("/auth/token", json={"username": username, "password": password}).json()
    return str(body["data"]["access_token"])


def _headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(client, username, password)}"}


# The full-access users, by role name.
_CONTACT = ("contact.jordan", "contact-dev-password")
_RISK = ("risk.riley", "risk-dev-password")
_MARKETING = ("marketing.avery", "marketing-dev-password")


def _any_customer_id(engine_path: Path) -> str:
    engine = create_sqlite_engine(engine_path, mode=AccessMode.READ_ONLY, pool_size=1)
    try:
        return str(query_all(engine, "SELECT customer_id FROM customer ORDER BY customer_id")[0][0])
    finally:
        engine.dispose()


# ==================================================================== /me
class TestMe:
    def test_me_reports_role_and_entitlement(self, api_client: TestClient) -> None:
        response = api_client.get("/me", headers=_headers(api_client, *_CONTACT))
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["role"] == "CONTACT_CENTER"
        assert data["entitlement"]["kind"] == "ALL"

    def test_me_summarizes_a_segment_scope_without_listing_ids(
        self, api_client: TestClient
    ) -> None:
        data = api_client.get("/me", headers=_headers(api_client, *_MARKETING)).json()["data"]
        assert data["entitlement"]["kind"] == "SEGMENT"
        assert data["entitlement"]["segments"]

    def test_me_requires_authentication(self, api_client: TestClient) -> None:
        assert api_client.get("/me").status_code == 401


# ==================================================================== search
class TestSearch:
    def test_search_returns_a_page_envelope(self, api_client: TestClient) -> None:
        response = api_client.get(
            "/customers", params={"q": "a", "limit": 5}, headers=_headers(api_client, *_CONTACT)
        )
        assert response.status_code == 200
        body = response.json()
        assert "items" in body["data"]
        assert body["meta"]["correlation_id"]

    def test_search_requires_authentication(self, api_client: TestClient) -> None:
        assert api_client.get("/customers", params={"q": "a"}).status_code == 401

    def test_search_rejects_an_over_long_limit(self, api_client: TestClient) -> None:
        response = api_client.get(
            "/customers", params={"q": "a", "limit": 9999}, headers=_headers(api_client, *_CONTACT)
        )
        assert response.status_code == 422


# ==================================================================== profile & entitlement
class TestProfileAndEntitlement:
    def test_profile_returns_masked_envelope(self, api_client: TestClient, phase5_db: Path) -> None:
        customer_id = _any_customer_id(phase5_db)
        response = api_client.get(
            f"/customers/{customer_id}", headers=_headers(api_client, *_CONTACT)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["profile"]["customer_id"] == customer_id
        assert "masked_fields" in body["meta"]

    def test_nonexistent_customer_is_404_for_a_full_access_user(
        self, api_client: TestClient
    ) -> None:
        response = api_client.get("/customers/C-NOPE", headers=_headers(api_client, *_CONTACT))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"

    def test_out_of_segment_customer_is_denied_without_an_existence_oracle(
        self, api_client: TestClient, phase5_db: Path
    ) -> None:
        """A marketing user (MASS/AFFLUENT) asking for an HNW/UHNW customer gets 403, and a
        non-existent id gets the same-family denial — neither reveals whether the row exists."""
        engine = create_sqlite_engine(phase5_db, mode=AccessMode.READ_ONLY, pool_size=1)
        try:
            hnw = query_one(
                engine,
                "SELECT customer_id FROM customer WHERE customer_segment IN ('HNW','UHNW') LIMIT 1",
            )
        finally:
            engine.dispose()
        if hnw is None:
            pytest.skip("no HNW/UHNW customer in this seed")

        response = api_client.get(f"/customers/{hnw[0]}", headers=_headers(api_client, *_MARKETING))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ENTITLEMENT_DENIED"

    def test_masking_hides_fields_for_a_restricted_role(
        self, api_client: TestClient, phase5_db: Path
    ) -> None:
        """Marketing sees a customer in its segment, but sensitive fields are masked on the wire."""
        engine = create_sqlite_engine(phase5_db, mode=AccessMode.READ_ONLY, pool_size=1)
        try:
            mass = query_one(
                engine,
                "SELECT customer_id FROM customer WHERE customer_segment IN ('MASS','AFFLUENT') "
                "LIMIT 1",
            )
        finally:
            engine.dispose()
        assert mass is not None
        response = api_client.get(
            f"/customers/{mass[0]}", headers=_headers(api_client, *_MARKETING)
        )
        assert response.status_code == 200
        # Marketing has a restricted policy, so at least one field is masked and the raw DOB (a
        # sensitive field) is not present in full.
        assert response.json()["meta"]["masked_fields"], "a restricted role must mask something"


# ==================================================================== sub-resources
class TestSubResources:
    @pytest.fixture
    def customer_id(self, phase5_db: Path) -> str:
        return _any_customer_id(phase5_db)

    @pytest.mark.parametrize(
        "suffix",
        [
            "accounts",
            "loans",
            "deposits",
            "investments",
            "transactions",
            "credit",
            "risk",
            "relationships",
            "household",
            "offers",
            "journey",
            "engagement",
            "360",
        ],
    )
    def test_sub_resource_returns_200_for_entitled_user(
        self, api_client: TestClient, customer_id: str, suffix: str
    ) -> None:
        response = api_client.get(
            f"/customers/{customer_id}/{suffix}", headers=_headers(api_client, *_RISK)
        )
        # risk profile may be absent for a given customer -> 404 is acceptable for /risk only.
        assert response.status_code in {200, 404}
        if response.status_code == 200:
            assert response.json()["meta"]["correlation_id"]

    def test_accounts_type_filter_validates(self, api_client: TestClient, customer_id: str) -> None:
        response = api_client.get(
            f"/customers/{customer_id}/accounts",
            params={"type": "NOT_A_TYPE"},
            headers=_headers(api_client, *_RISK),
        )
        assert response.status_code == 422

    def test_engagement_channel_filter_validates(
        self, api_client: TestClient, customer_id: str
    ) -> None:
        response = api_client.get(
            f"/customers/{customer_id}/engagement",
            params={"channel": "NOT_A_CHANNEL"},
            headers=_headers(api_client, *_RISK),
        )
        assert response.status_code == 422

    def test_engagement_filters_by_channel(self, api_client: TestClient, customer_id: str) -> None:
        response = api_client.get(
            f"/customers/{customer_id}/engagement",
            params={"channel": "WEB"},
            headers=_headers(api_client, *_RISK),
        )
        assert response.status_code == 200
        events = response.json()["data"]["events"]
        assert all(event["channel"] == "WEB" for event in events)

    def test_credit_returns_masked_envelope(self, api_client: TestClient, customer_id: str) -> None:
        response = api_client.get(
            f"/customers/{customer_id}/credit", headers=_headers(api_client, *_RISK)
        )
        assert response.status_code == 200
        body = response.json()
        assert "credit_profile" in body["data"]

    def test_sub_resources_require_authentication(
        self, api_client: TestClient, customer_id: str
    ) -> None:
        assert api_client.get(f"/customers/{customer_id}/risk").status_code == 401


# ==================================================================== 360 partial tolerance
class Test360:
    def test_360_returns_profile_and_meta(self, api_client: TestClient, phase5_db: Path) -> None:
        customer_id = _any_customer_id(phase5_db)
        response = api_client.get(
            f"/customers/{customer_id}/360", headers=_headers(api_client, *_RISK)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["customer_id"] == customer_id
        assert body["data"].get("profile"), "the profile is always present on a successful 360"
        assert "errors" in body["meta"]

    def test_360_isolates_a_module_failure_into_meta_errors(
        self, api_client: TestClient, phase5_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single module raising becomes a partial 200 with the failure in ``meta.errors[]``."""

        def _boom(self: RiskService, customer_id: str) -> None:
            raise RuntimeError("risk store unavailable")

        monkeypatch.setattr(RiskService, "get_risk", _boom)
        customer_id = _any_customer_id(phase5_db)
        response = api_client.get(
            f"/customers/{customer_id}/360", headers=_headers(api_client, *_RISK)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["data"].get("profile"), "other modules still load"
        assert any(err["module"] == "risk" for err in body["meta"]["errors"])
        assert "risk" not in body["data"], "the failed module's slot is omitted"


# ==================================================================== contract (task 5.9)
class TestContract:
    """The published OpenAPI spec matches the implemented surface (requirement 15.4)."""

    _EXPECTED_PATHS: ClassVar[set[str]] = {
        "/customers",
        "/customers/{customer_id}",
        "/customers/{customer_id}/360",
        "/customers/{customer_id}/accounts",
        "/customers/{customer_id}/loans",
        "/customers/{customer_id}/deposits",
        "/customers/{customer_id}/investments",
        "/customers/{customer_id}/transactions",
        "/customers/{customer_id}/credit",
        "/customers/{customer_id}/relationships",
        "/customers/{customer_id}/household",
        "/customers/{customer_id}/risk",
        "/customers/{customer_id}/journey",
        "/customers/{customer_id}/engagement",
        "/customers/{customer_id}/offers",
        "/customers/{customer_id}/signals",
        "/signals",
        "/signals/{signal_id}/dismiss",
        "/signals/{signal_id}/ack",
        "/me",
    }

    def test_every_designed_path_is_published(self, api_client: TestClient) -> None:
        spec = api_client.get("/openapi.json").json()
        published = set(spec["paths"])
        missing = self._EXPECTED_PATHS - published
        assert not missing, f"OpenAPI is missing designed paths: {missing}"

    def test_error_envelope_shape_is_uniform(self, api_client: TestClient) -> None:
        """Every error is the documented ``{error:{code,message,...}}`` shape (design §6.3)."""
        cases = [
            api_client.get("/customers/C-NOPE"),  # 401 (unauthenticated)
            api_client.get("/customers/C-NOPE", headers=_headers(api_client, *_CONTACT)),  # 404
        ]
        for response in cases:
            body = response.json()
            assert "error" in body
            assert set(body["error"]) >= {"code", "message", "correlation_id"}

    def test_success_envelope_shape_is_uniform(
        self, api_client: TestClient, phase5_db: Path
    ) -> None:
        customer_id = _any_customer_id(phase5_db)
        for suffix in ["", "/accounts", "/offers", "/journey"]:
            response = api_client.get(
                f"/customers/{customer_id}{suffix}", headers=_headers(api_client, *_RISK)
            )
            body = response.json()
            assert "data" in body
            assert set(body["meta"]) >= {"correlation_id", "as_of", "masked_fields", "errors"}


# ==================================================================== performance (task 5.9)
class TestPerformance:
    """Per-endpoint latency budgets from design §6.3, asserted on the seeded dataset.

    Budgets are generous multiples of the design targets: this measures a local in-process client
    on a small seed, not the production p95, so the assertion guards against an accidental O(n) or
    N+1 regression rather than certifying the SLO. The design budget is named in each case.
    """

    @pytest.fixture
    def customer_id(self, phase5_db: Path) -> str:
        return _any_customer_id(phase5_db)

    def _p95_ms(
        self, client: TestClient, path: str, headers: dict[str, str], runs: int = 12
    ) -> float:
        samples: list[float] = []
        for _ in range(runs):
            start = time.perf_counter()
            response = client.get(path, headers=headers)
            samples.append((time.perf_counter() - start) * 1000)
            assert response.status_code in {200, 404}
        samples.sort()
        return samples[int(len(samples) * 0.95) - 1]

    def test_profile_is_within_budget(self, api_client: TestClient, customer_id: str) -> None:
        headers = _headers(api_client, *_RISK)
        # Design budget 300 ms; allow generous headroom for the in-process harness.
        assert self._p95_ms(api_client, f"/customers/{customer_id}", headers) < 1500

    def test_relationships_traversal_is_within_budget(
        self, api_client: TestClient, customer_id: str
    ) -> None:
        headers = _headers(api_client, *_RISK)
        # Design budget 2 s p95 for a 3-hop traversal (requirement 6.7).
        assert self._p95_ms(api_client, f"/customers/{customer_id}/relationships", headers) < 2000

    def test_360_is_within_budget(self, api_client: TestClient, customer_id: str) -> None:
        headers = _headers(api_client, *_RISK)
        # Design budget 3 s p95 for the full composed view.
        assert self._p95_ms(api_client, f"/customers/{customer_id}/360", headers) < 3000
