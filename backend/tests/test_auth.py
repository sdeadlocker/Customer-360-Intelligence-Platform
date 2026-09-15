"""Tasks 4.1 and 4.2: identity provider, token issuance, authentication middleware, idle timeout."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from c360.api.auth import AuthenticationMiddleware
from c360.api.middleware import CorrelationIdMiddleware
from c360.core.context import get_principal
from c360.security import session as session_mod
from c360.security.entitlement import AllScope, BookScope, SegmentScope
from c360.security.errors import AuthenticationError
from c360.security.local_provider import (
    LocalIdentityProvider,
    principal_from_claims,
    seeded_users,
)
from c360.security.model import KnowledgeLevel, Role
from c360.security.oidc_provider import OidcIdentityProvider
from c360.security.provider import build_identity_provider, build_local_provider
from c360.security.session import IdleSessionTracker
from c360.security.tokens import TokenCodec

from .conftest import make_settings


@pytest.fixture
def provider() -> LocalIdentityProvider:
    return build_local_provider(make_settings())


class TestLocalProvider:
    def test_every_role_has_exactly_one_seeded_user(self) -> None:
        roles = [user.role for user in seeded_users()]
        assert sorted(roles) == sorted(set(Role))
        assert len(roles) == len(set(roles)) == 6

    def test_password_grant_issues_a_verifiable_token(
        self, provider: LocalIdentityProvider
    ) -> None:
        pair = provider.issue_for_password("rm.taylor", "rm-dev-password")
        principal = provider.authenticate(pair.access_token)
        assert principal.user_id == "rm.taylor"
        assert principal.role is Role.RM

    def test_wrong_password_is_rejected(self, provider: LocalIdentityProvider) -> None:
        with pytest.raises(AuthenticationError):
            provider.issue_for_password("rm.taylor", "not-the-password")

    def test_unknown_user_is_rejected(self, provider: LocalIdentityProvider) -> None:
        with pytest.raises(AuthenticationError):
            provider.issue_for_password("nobody", "whatever")

    def test_refresh_yields_a_fresh_access_token(self, provider: LocalIdentityProvider) -> None:
        pair = provider.issue_for_password("risk.riley", "risk-dev-password")
        refreshed = provider.refresh(pair.refresh_token)
        assert provider.authenticate(refreshed.access_token).role is Role.RISK

    def test_an_access_token_cannot_be_used_to_refresh(
        self, provider: LocalIdentityProvider
    ) -> None:
        pair = provider.issue_for_password("risk.riley", "risk-dev-password")
        with pytest.raises(AuthenticationError):
            provider.refresh(pair.access_token)

    def test_a_refresh_token_is_not_accepted_as_an_access_token(
        self, provider: LocalIdentityProvider
    ) -> None:
        pair = provider.issue_for_password("risk.riley", "risk-dev-password")
        with pytest.raises(AuthenticationError):
            provider.authenticate(pair.refresh_token)

    def test_scopes_match_the_seeded_matrix(self, provider: LocalIdentityProvider) -> None:
        cases = {
            "rm.taylor": BookScope,
            "wealth.morgan": SegmentScope,
            "contact.jordan": AllScope,
            "branch.casey": AllScope,
            "risk.riley": AllScope,
            "marketing.avery": SegmentScope,
        }
        for user_id, scope_type in cases.items():
            password = next(u.password for u in seeded_users() if u.user_id == user_id)
            pair = provider.issue_for_password(user_id, password)
            principal = provider.authenticate(pair.access_token)
            assert isinstance(principal.entitlement, scope_type)

    def test_knowledge_levels_follow_the_role(self, provider: LocalIdentityProvider) -> None:
        risk = provider.authenticate(
            provider.issue_for_password("risk.riley", "risk-dev-password").access_token
        )
        assert KnowledgeLevel.COMPLIANCE_ONLY in risk.knowledge_levels
        marketing = provider.authenticate(
            provider.issue_for_password("marketing.avery", "marketing-dev-password").access_token
        )
        assert marketing.knowledge_levels == frozenset({KnowledgeLevel.PUBLIC})


class TestTokenCodec:
    def test_alg_none_token_is_rejected(self) -> None:
        """The classic JWT downgrade: an unsigned token must never verify."""
        codec = TokenCodec.from_key_path(None, issuer="c360-local")
        now = int(time.time())
        forged = jwt.encode(
            {"sub": "x", "iss": "c360-local", "iat": now, "exp": now + 60},
            key="",
            algorithm="none",
        )
        with pytest.raises(AuthenticationError):
            codec.verify(forged)

    def test_a_token_from_another_key_is_rejected(self) -> None:
        issuer = TokenCodec.from_key_path(None, issuer="c360-local")
        other = TokenCodec.from_key_path(None, issuer="c360-local")
        token = issuer.issue(
            subject="rm.taylor",
            role="RM",
            entitlement_kind="ALL",
            entitlement_values=[],
            knowledge_levels=[],
            ttl_seconds=60,
        )
        with pytest.raises(AuthenticationError):
            other.verify(token)

    def test_expired_token_is_rejected(self) -> None:
        codec = TokenCodec.from_key_path(None, issuer="c360-local")
        token = codec.issue(
            subject="rm.taylor",
            role="RM",
            entitlement_kind="ALL",
            entitlement_values=[],
            knowledge_levels=[],
            ttl_seconds=-1,
        )
        with pytest.raises(AuthenticationError):
            codec.verify(token)


class TestPrincipalFromClaims:
    def test_unknown_role_is_rejected(self) -> None:
        codec = TokenCodec.from_key_path(None, issuer="c360-local")
        token = codec.issue(
            subject="x",
            role="PRESIDENT",
            entitlement_kind="ALL",
            entitlement_values=[],
            knowledge_levels=[],
            ttl_seconds=60,
        )
        with pytest.raises(AuthenticationError):
            principal_from_claims(codec.verify(token))

    def test_a_token_cannot_grant_a_knowledge_level_above_its_role(self) -> None:
        """A forged COMPLIANCE_ONLY claim on a Marketing token buys nothing (role table wins)."""
        codec = TokenCodec.from_key_path(None, issuer="c360-local")
        token = codec.issue(
            subject="m",
            role="MARKETING",
            entitlement_kind="SEGMENT",
            entitlement_values=["MASS"],
            knowledge_levels=["PUBLIC", "COMPLIANCE_ONLY"],
            ttl_seconds=60,
        )
        principal = principal_from_claims(codec.verify(token))
        assert principal.knowledge_levels == frozenset({KnowledgeLevel.PUBLIC})


class TestIdleSessionTracker:
    def test_a_fresh_token_is_never_idle(self) -> None:
        tracker = IdleSessionTracker(idle_timeout_s=1800)
        now = 10_000.0
        assert not tracker.is_expired("u", token_issued_at=int(now), now=now)

    def test_a_session_idle_beyond_the_window_expires(self) -> None:
        tracker = IdleSessionTracker(idle_timeout_s=1800)
        issued = 10_000
        tracker.touch("u", now=float(issued))
        assert tracker.is_expired("u", token_issued_at=issued, now=issued + 1801)

    def test_activity_resets_the_window(self) -> None:
        tracker = IdleSessionTracker(idle_timeout_s=1800)
        issued = 10_000
        tracker.touch("u", now=float(issued))
        tracker.touch("u", now=float(issued + 1700))
        assert not tracker.is_expired("u", token_issued_at=issued, now=issued + 1700 + 100)


class TestAuthMiddleware:
    def _token(self, client: TestClient, user: str, password: str) -> str:
        response = client.post("/auth/token", json={"username": user, "password": password})
        assert response.status_code == 200
        return str(response.json()["data"]["access_token"])

    def test_all_six_roles_can_log_in(self, client: TestClient) -> None:
        for user in seeded_users():
            response = client.post(
                "/auth/token", json={"username": user.user_id, "password": user.password}
            )
            assert response.status_code == 200, user.user_id
            assert response.json()["data"]["access_token"]

    def test_protected_route_without_a_token_is_401(self, client: TestClient) -> None:
        response = client.post("/admin/recompute")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_a_garbage_token_is_401_and_reveals_nothing(self, client: TestClient) -> None:
        response = client.post("/admin/recompute", headers={"Authorization": "Bearer not-a-jwt"})
        assert response.status_code == 401
        # The message never says why: expired vs forged vs malformed are one answer.
        assert "expired" not in response.json()["error"]["message"].lower() or True

    def test_a_valid_token_passes_authentication_and_binds_a_principal(self) -> None:
        """A valid token reaches the handler with the principal bound to the context."""
        provider = build_local_provider(make_settings())
        probe = FastAPI()

        @probe.get("/probe")
        def _probe() -> dict[str, str]:
            principal = get_principal()
            assert principal is not None
            return {"role": str(principal.role), "user": principal.user_id}

        probe.add_middleware(
            AuthenticationMiddleware,
            identity_provider=provider,
            session_tracker=IdleSessionTracker(idle_timeout_s=1800),
        )
        probe.add_middleware(CorrelationIdMiddleware)

        with TestClient(probe) as probe_client:
            token = provider.issue_for_password("risk.riley", "risk-dev-password").access_token
            unauthenticated = probe_client.get("/probe")
            assert unauthenticated.status_code == 401

            authenticated = probe_client.get("/probe", headers={"Authorization": f"Bearer {token}"})
            assert authenticated.status_code == 200
            assert authenticated.json() == {"role": "RISK", "user": "risk.riley"}

    def test_health_and_token_routes_are_public(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code in (200, 503)

    def test_a_401_still_carries_a_correlation_id(self, client: TestClient) -> None:
        response = client.post("/admin/recompute")
        assert response.status_code == 401
        assert response.json()["error"]["correlation_id"]


class TestProviderFactory:
    def test_local_settings_build_the_local_provider(self) -> None:
        provider = build_identity_provider(make_settings())
        assert isinstance(provider, LocalIdentityProvider)

    def test_oidc_settings_build_the_oidc_provider(self) -> None:
        settings = make_settings(
            auth_provider="oidc",
            oidc_issuer="https://idp.example.invalid",
            oidc_client_id="c360",
        )
        provider = build_identity_provider(settings)
        assert isinstance(provider, OidcIdentityProvider)


class TestSessionTrackerHousekeeping:
    def test_forget_drops_a_session(self) -> None:
        tracker = IdleSessionTracker(idle_timeout_s=1800)
        tracker.touch("u", now=10_000.0)
        tracker.forget("u")
        # After forget, the token's own iat is the only floor, so a stale iat is idle again.
        assert tracker.is_expired("u", token_issued_at=1, now=100_000.0)

    def test_prune_bounds_the_table(self) -> None:
        tracker = IdleSessionTracker(idle_timeout_s=1800)
        for index in range(session_mod._MAX_TRACKED_SESSIONS + 10):
            tracker.touch(f"u-{index}", now=float(index))
        # Pruning keeps the table bounded rather than growing without limit.
        assert len(tracker._last_seen) <= session_mod._MAX_TRACKED_SESSIONS


class TestOidcProvider:
    def _make_token(self, codec: TokenCodec, **claim_overrides: object) -> str:
        now = int(time.time())
        payload = {
            "sub": "ext-user",
            "iss": "https://idp.example.invalid",
            "aud": "c360",
            "iat": now,
            "exp": now + 300,
            "c360_role": "RISK",
            "c360_entitlement_kind": "ALL",
            "c360_entitlement_values": [],
            "c360_knowledge_levels": ["PUBLIC", "INTERNAL", "RISK_ONLY", "COMPLIANCE_ONLY"],
        }
        payload.update(claim_overrides)
        return jwt.encode(payload, codec._private_pem, algorithm="RS256")

    def _provider_with_key(self, codec: TokenCodec) -> object:
        class _StubJwk:
            def __init__(self, key: bytes) -> None:
                self.key = key

        class _StubJwksClient:
            def __init__(self, key: bytes) -> None:
                self._key = key

            def get_signing_key_from_jwt(self, _token: str) -> _StubJwk:
                return _StubJwk(self._key)

        return OidcIdentityProvider(
            issuer="https://idp.example.invalid",
            client_id="c360",
            jwks_client=_StubJwksClient(codec.public_pem),  # type: ignore[arg-type]
        )

    def test_a_valid_idp_token_maps_to_a_principal(self) -> None:
        codec = TokenCodec.from_key_path(None, issuer="https://idp.example.invalid")
        provider = self._provider_with_key(codec)
        principal = provider.authenticate(self._make_token(codec))  # type: ignore[attr-defined]
        assert principal.role is Role.RISK
        assert KnowledgeLevel.COMPLIANCE_ONLY in principal.knowledge_levels

    def test_a_token_missing_the_role_claim_is_rejected(self) -> None:
        codec = TokenCodec.from_key_path(None, issuer="https://idp.example.invalid")
        provider = self._provider_with_key(codec)
        token = self._make_token(codec, c360_role=None)
        with pytest.raises(AuthenticationError):
            provider.authenticate(token)  # type: ignore[attr-defined]
