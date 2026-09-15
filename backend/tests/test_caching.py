"""Caching-tier tests (task 16.2, design §13.1).

Three things are proven here:

* the shared :class:`c360.core.cache.TtlCache` honours its TTL, its zero-TTL disable, and its
  single-key and whole-store invalidation — the get/put/invalidate contract every tier shares;
* :func:`c360.api.caching.conditional_response` emits a strong ``ETag`` and a ``Cache-Control`` on a
  first read and a bodyless ``304`` when the client presents a matching ``If-None-Match`` — so
  reference data can be revalidated cheaply;
* the read-model cache is wired onto app state and cleared by the recompute path, so a recompute
  never leaves a stale read model behind.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from c360.api.caching import content_etag
from c360.api.readiness import REGISTRY
from c360.core.cache import TtlCache
from c360.core.logging import configure_logging
from c360.knowledge.extension import vec_available
from c360.knowledge.ingest import ingest_knowledge
from c360.main import create_app
from tests.conftest import make_settings

_DIMENSIONS = 256


# ==================================================================== TtlCache
class TestTtlCache:
    def test_put_then_get_round_trips(self) -> None:
        cache: TtlCache[str] = TtlCache(ttl_s=60)
        cache.put("k", "v")
        assert cache.get("k") == "v"

    def test_missing_key_is_none(self) -> None:
        cache: TtlCache[str] = TtlCache(ttl_s=60)
        assert cache.get("absent") is None

    def test_entry_expires(self) -> None:
        cache: TtlCache[str] = TtlCache(ttl_s=0.05)
        cache.put("k", "v")
        assert cache.get("k") == "v"
        time.sleep(0.08)
        assert cache.get("k") is None

    def test_zero_ttl_disables_the_cache(self) -> None:
        cache: TtlCache[str] = TtlCache(ttl_s=0)
        cache.put("k", "v")
        assert cache.get("k") is None

    def test_invalidate_drops_one_key(self) -> None:
        cache: TtlCache[str] = TtlCache(ttl_s=60)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.invalidate("a")
        assert cache.get("a") is None
        assert cache.get("b") == "2"

    def test_invalidate_all_clears_and_counts(self) -> None:
        cache: TtlCache[str] = TtlCache(ttl_s=60)
        cache.put("a", "1")
        cache.put("b", "2")
        assert cache.invalidate_all() == 2
        assert cache.get("a") is None
        assert cache.get("b") is None


# ==================================================================== ETag derivation
class TestContentEtag:
    def test_equal_content_hashes_equal_regardless_of_key_order(self) -> None:
        assert content_etag({"a": 1, "b": 2}) == content_etag({"b": 2, "a": 1})

    def test_different_content_hashes_differently(self) -> None:
        assert content_etag({"a": 1}) != content_etag({"a": 2})

    def test_tag_is_a_quoted_strong_validator(self) -> None:
        tag = content_etag({"a": 1})
        assert tag.startswith('"')
        assert tag.endswith('"')


# ==================================================================== HTTP conditional response
@pytest.fixture(scope="module")
def knowledge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not vec_available():
        pytest.skip("sqlite-vec extension unavailable in this environment")
    path = tmp_path_factory.mktemp("cache_kb") / "knowledge.db"
    ingest_knowledge(database=path, dimensions=_DIMENSIONS)
    return path


@pytest.fixture
def kb_app(
    knowledge_db: Path, phase5_db: Path, tmp_path_factory: pytest.TempPathFactory
) -> FastAPI:
    REGISTRY.clear()
    audit_dir = tmp_path_factory.mktemp("cache_audit")
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


def _headers(client: TestClient) -> dict[str, str]:
    body = client.post(
        "/auth/token", json={"username": "rm.taylor", "password": "rm-dev-password"}
    ).json()
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


class TestReferenceDataCaching:
    def test_document_response_carries_etag_and_cache_control(self, kb_client: TestClient) -> None:
        response = kb_client.get("/knowledge/documents/pol-overdraft", headers=_headers(kb_client))
        assert response.status_code == 200
        assert response.headers["etag"]
        assert "max-age=60" in response.headers["cache-control"]
        assert "must-revalidate" in response.headers["cache-control"]

    def test_matching_if_none_match_returns_304(self, kb_client: TestClient) -> None:
        headers = _headers(kb_client)
        first = kb_client.get("/knowledge/documents/pol-overdraft", headers=headers)
        etag = first.headers["etag"]

        second = kb_client.get(
            "/knowledge/documents/pol-overdraft",
            headers={**headers, "If-None-Match": etag},
        )
        assert second.status_code == 304
        assert second.headers["etag"] == etag
        assert second.content == b""

    def test_etag_is_stable_across_requests(self, kb_client: TestClient) -> None:
        """The per-request correlation/trace id in meta must not leak into the tag."""
        headers = _headers(kb_client)
        a = kb_client.get("/knowledge/documents/pol-overdraft", headers=headers)
        b = kb_client.get("/knowledge/documents/pol-overdraft", headers=headers)
        assert a.headers["etag"] == b.headers["etag"]


# ==================================================================== read-model cache wiring
class TestReadModelCacheWiring:
    def test_read_model_cache_is_on_app_state(self, kb_app: FastAPI) -> None:
        cache = kb_app.state.read_model_cache
        assert isinstance(cache, TtlCache)

    def test_recompute_invalidates_the_read_model_cache(
        self, phase5_db: Path, tmp_path: Path
    ) -> None:
        REGISTRY.clear()
        # A private copy of the seeded DB so recompute's write does not touch the shared fixture.
        db = tmp_path / "customer.db"
        shutil.copy(phase5_db, db)
        settings = make_settings(
            sqlite_db_path=str(db),
            sqlite_audit_db_path=str(tmp_path / "audit.db"),
        )
        configure_logging(settings, force=True)
        app = create_app(settings)
        with TestClient(app) as client:
            cache = app.state.read_model_cache
            cache.put("probe", object())
            assert cache.get("probe") is not None

            token = client.post(
                "/auth/token", json={"username": "risk.riley", "password": "risk-dev-password"}
            ).json()["data"]["access_token"]
            response = client.post("/admin/recompute", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            # The recompute path clears the read-model cache alongside the agent cache (task 16.2).
            assert cache.get("probe") is None
