"""Task 0.3: typed configuration, fail-fast validation and startup advisories."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from c360.core.config import (
    PROJECT_ROOT,
    AuthProvider,
    ConfigurationError,
    Environment,
    LlmProvider,
    Settings,
    get_settings,
    reset_settings_cache,
)
from tests.conftest import make_settings


class TestDefaults:
    def test_every_design_18_variable_is_represented(self) -> None:
        """Guards against a §18 variable being read from os.environ somewhere instead."""
        expected = {
            # data
            "sqlite_db_path",
            "sqlite_audit_db_path",
            "sqlite_checkpoint_db_path",
            "sqlite_knowledge_db_path",
            "sqlite_eval_db_path",
            "sqlite_read_pool_size",
            "seed_customer_count",
            "seed_random_seed",
            # bedrock
            "llm_provider",
            "aws_region",
            "bedrock_model_id",
            "bedrock_embed_model_id",
            "bedrock_embed_dimensions",
            "bedrock_guardrail_id",
            "bedrock_max_tokens",
            "bedrock_read_timeout_s",
            "bedrock_max_retries",
            # retrieval
            "retrieval_enabled",
            "retrieval_lexical_k",
            "retrieval_semantic_k",
            "retrieval_rrf_k",
            "retrieval_context_chunks",
            "retrieval_min_score",
            "rerank_enabled",
            "rerank_model_id",
            "chunk_target_tokens",
            "chunk_overlap_ratio",
            # agents
            "agent_cache_ttl_s",
            "agent_wave1_budget_s",
            "agent_wave2_budget_s",
            "agent_wave3_budget_s",
            "qa_max_tool_calls",
            "prompt_registry_path",
            # graph
            "graph_max_hops",
            "graph_node_cap",
            # thresholds
            "major_txn_absolute_threshold_cents",
            "major_txn_median_multiple",
            "spend_anomaly_sigma",
            "offer_cooling_off_days",
            # observability
            "otel_enabled",
            "otel_exporter_otlp_endpoint",
            "otel_service_name",
            "otel_traces_sampler_arg",
            "otel_capture_prompt_content",
            "telemetry_customer_hash_salt",
            "cost_price_table_path",
            # evaluation
            "eval_panel_per_cohort",
            "eval_groundedness_min",
            "eval_coverage_min",
            "eval_retrieval_recall_min",
            "eval_regression_tolerance_pts",
            # auth
            "auth_provider",
            "jwt_private_key_path",
            "jwt_access_ttl_s",
            "session_idle_timeout_s",
            "oidc_issuer",
            "oidc_client_id",
            "audit_retention_days",
        }
        assert expected <= set(Settings.model_fields)

    def test_documented_defaults(self) -> None:
        settings = make_settings(llm_provider="mock")
        assert settings.seed_customer_count == 100
        assert settings.seed_random_seed == 42
        assert settings.sqlite_read_pool_size == 8
        assert settings.bedrock_embed_model_id == "amazon.titan-embed-text-v2:0"
        assert settings.bedrock_embed_dimensions == 1024
        assert settings.retrieval_rrf_k == 60
        assert settings.retrieval_min_score == pytest.approx(0.02)
        assert settings.rerank_enabled is False
        assert settings.chunk_target_tokens == 500
        assert settings.chunk_overlap_ratio == pytest.approx(0.15)
        assert settings.agent_cache_ttl_s == 900
        assert settings.read_model_cache_ttl_s == 60
        assert settings.qa_max_tool_calls == 6
        assert settings.graph_max_hops == 3
        assert settings.graph_node_cap == 300
        assert settings.major_txn_absolute_threshold_cents == 1_000_000
        assert settings.major_txn_median_multiple == pytest.approx(10.0)
        assert settings.spend_anomaly_sigma == pytest.approx(2.0)
        assert settings.offer_cooling_off_days == 90
        assert settings.session_idle_timeout_s == 1800
        assert settings.jwt_access_ttl_s == 900
        assert settings.audit_retention_days == 365

    def test_prompt_content_capture_is_off_by_default(self) -> None:
        assert make_settings().otel_capture_prompt_content is False

    def test_settings_are_immutable(self) -> None:
        settings = make_settings()
        with pytest.raises(ValidationError):
            settings.seed_customer_count = 500  # type: ignore[misc]


class TestFailFast:
    def test_bedrock_provider_requires_model_id(self) -> None:
        with pytest.raises(ConfigurationError, match="BEDROCK_MODEL_ID"):
            make_settings(llm_provider="bedrock", bedrock_model_id="")

    def test_bedrock_provider_accepts_a_model_id(self) -> None:
        settings = make_settings(llm_provider="bedrock", bedrock_model_id="a.model-id:0")
        assert settings.llm_provider is LlmProvider.BEDROCK

    def test_rerank_requires_a_model_id(self) -> None:
        with pytest.raises(ConfigurationError, match="RERANK_MODEL_ID"):
            make_settings(rerank_enabled="true", rerank_model_id="")

    def test_oidc_requires_issuer_and_client_id(self) -> None:
        with pytest.raises(ConfigurationError, match="OIDC_ISSUER"):
            make_settings(auth_provider="oidc")
        with pytest.raises(ConfigurationError, match="OIDC_CLIENT_ID"):
            make_settings(auth_provider="oidc", oidc_issuer="https://issuer.example")

    def test_otel_requires_an_endpoint(self) -> None:
        with pytest.raises(ConfigurationError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
            make_settings(otel_enabled="true", otel_exporter_otlp_endpoint="")

    def test_missing_jwt_key_file_is_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigurationError, match="JWT_PRIVATE_KEY_PATH"):
            make_settings(jwt_private_key_path=str(tmp_path / "absent.pem"))

    def test_existing_jwt_key_file_is_accepted(self, tmp_path) -> None:
        key = tmp_path / "dev.pem"
        key.write_text("not-a-real-key", encoding="utf-8")
        settings = make_settings(jwt_private_key_path=str(key))
        assert settings.auth_provider is AuthProvider.LOCAL

    def test_context_chunks_cannot_exceed_candidate_pool(self) -> None:
        with pytest.raises(ConfigurationError, match="RETRIEVAL_CONTEXT_CHUNKS"):
            make_settings(
                retrieval_lexical_k=3,
                retrieval_semantic_k=3,
                retrieval_context_chunks=5,
            )

    def test_prompt_capture_is_refused_outside_development(self) -> None:
        with pytest.raises(ConfigurationError, match="OTEL_CAPTURE_PROMPT_CONTENT"):
            make_settings(environment="prod", otel_capture_prompt_content="true")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("bedrock_embed_dimensions", "768"),
            ("otel_traces_sampler_arg", "1.5"),
            ("chunk_overlap_ratio", "0.9"),
            ("sqlite_read_pool_size", "0"),
            ("graph_max_hops", "9"),
            ("log_level", "TRACE"),
            ("seed_customer_count", "0"),
            ("session_idle_timeout_s", "5"),
        ],
    )
    def test_out_of_range_values_are_rejected(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError):
            make_settings(**{field: value})


class TestPathResolution:
    def test_relative_paths_resolve_against_the_repository_root(self) -> None:
        settings = make_settings()
        assert settings.customer_db_path == PROJECT_ROOT / "data" / "customer.db"
        assert settings.knowledge_db_path == PROJECT_ROOT / "data" / "knowledge.db"
        assert settings.prompt_registry_dir == PROJECT_ROOT / "prompts"
        assert settings.cost_price_table == PROJECT_ROOT / "config" / "bedrock_prices.json"

    def test_absolute_paths_are_left_alone(self, tmp_path) -> None:
        target = tmp_path / "elsewhere.db"
        settings = make_settings(sqlite_db_path=str(target))
        assert settings.customer_db_path == target

    def test_project_root_is_the_repository_root(self) -> None:
        assert (PROJECT_ROOT / "backend" / "pyproject.toml").is_file()


class TestStartupWarnings:
    def test_prompt_capture_warns(self) -> None:
        warnings = make_settings(otel_capture_prompt_content="true").startup_warnings()
        assert any("OTEL_CAPTURE_PROMPT_CONTENT" in warning for warning in warnings)

    def test_missing_hash_salt_warns(self) -> None:
        warnings = make_settings(telemetry_customer_hash_salt="").startup_warnings()
        assert any("TELEMETRY_CUSTOMER_HASH_SALT is empty" in warning for warning in warnings)

    def test_short_hash_salt_warns(self) -> None:
        warnings = make_settings(telemetry_customer_hash_salt="short").startup_warnings()
        assert any("shorter than 16" in warning for warning in warnings)

    def test_mock_provider_warns(self) -> None:
        warnings = make_settings(llm_provider="mock").startup_warnings()
        assert any("LLM_PROVIDER=mock" in warning for warning in warnings)

    def test_disabled_telemetry_warns(self) -> None:
        warnings = make_settings(otel_enabled="false").startup_warnings()
        assert any("OTEL_ENABLED=false" in warning for warning in warnings)

    def test_a_clean_configuration_is_quiet(self) -> None:
        settings = make_settings(
            llm_provider="bedrock",
            bedrock_model_id="a.model-id:0",
            telemetry_customer_hash_salt="a-sufficiently-long-salt",
        )
        assert settings.startup_warnings() == []


class TestEnvironmentBinding:
    def test_values_come_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("SEED_CUSTOMER_COUNT", "1000")
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("ENVIRONMENT", "dev")
        reset_settings_cache()
        try:
            settings = get_settings()
            assert settings.seed_customer_count == 1000
            assert settings.environment is Environment.DEV
            assert get_settings() is settings, "settings must be a cached singleton"
        finally:
            reset_settings_cache()

    def test_unknown_variables_are_ignored(self, monkeypatch) -> None:
        monkeypatch.setenv("C360_SOMETHING_UNKNOWN", "value")
        assert make_settings().environment is Environment.LOCAL
