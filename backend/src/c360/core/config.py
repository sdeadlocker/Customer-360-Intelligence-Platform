"""Typed application configuration.

Every variable in design §18 is represented here exactly once. Configuration is validated at
import of :func:`get_settings`, which the application calls during startup, so a missing or
invalid required value fails the process rather than surfacing as a runtime error on the first
request that happens to need it.

No secret ever has a default. AWS credentials are not configuration at all — they come from the
default credential chain (`AWS_PROFILE`, instance role, environment), per the Phase 0 rules.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/c360/core/config.py -> core -> c360 -> src -> backend -> repository root
PROJECT_ROOT: Path = Path(__file__).resolve().parents[4]

#: Below this length the pseudonymous customer reference is weak against brute-force reversal.
MIN_HASH_SALT_LENGTH = 16


class LlmProvider(StrEnum):
    """Which :class:`LLMProvider` implementation to bind."""

    BEDROCK = "bedrock"
    MOCK = "mock"


class AuthProvider(StrEnum):
    """Which :class:`IdentityProvider` implementation to bind."""

    LOCAL = "local"
    OIDC = "oidc"


class Environment(StrEnum):
    """Deployment environment. Governs a small number of safety checks."""

    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class ConfigurationError(RuntimeError):
    """Raised when configuration is missing or internally inconsistent."""


Port = Annotated[int, Field(ge=1, le=65535)]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]


class Settings(BaseSettings):
    """Application settings, sourced from the environment and an optional ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
        frozen=True,
    )

    # ---------------------------------------------------------------- runtime
    environment: Environment = Environment.LOCAL
    api_host: str = "127.0.0.1"
    api_port: Port = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ---------------------------------------------------------------- data (§18)
    sqlite_db_path: Path = Path("data/customer.db")
    sqlite_audit_db_path: Path = Path("data/audit.db")
    sqlite_checkpoint_db_path: Path = Path("data/checkpoints.db")
    sqlite_knowledge_db_path: Path = Path("data/knowledge.db")
    sqlite_eval_db_path: Path = Path("data/eval.db")
    # Phase 17: the proactive-alerting store. A writable database, created and migrated the same
    # way as audit.db / checkpoints.db, holding signal instances and their per-user state. It is
    # never customer.db — signals are *derived* from the read-only customer database and its
    # derived tables, and the read-only rule on customer.db is thereby preserved.
    sqlite_signals_db_path: Path = Path("data/signals.db")
    # Phase 18: the scheduled-and-branded-report-exports store. A writable database, created and
    # migrated the same way as audit.db / checkpoints.db / signals.db, holding report definitions,
    # their schedules and the run history. It has no foreign key into customer.db — customers and
    # books are referenced by id only — so the read-only rule on customer.db is preserved. The
    # generated artifacts themselves are written to `reports_output_dir` on the data/ volume, never
    # into any database (task 18.1).
    sqlite_reports_db_path: Path = Path("data/reports.db")
    # Where generated report artifacts (PDF packs, briefings, digests and their manifests) are
    # written. On the data/ volume so a containerized deployment persists them alongside the
    # writable databases; resolved against the project root exactly as the database paths are.
    reports_output_dir: Path = Path("data/reports")
    sqlite_read_pool_size: Annotated[int, Field(ge=1, le=64)] = 8
    seed_customer_count: Annotated[int, Field(ge=1, le=1_000_000)] = 100
    seed_random_seed: int = 42

    # ---------------------------------------------------------------- bedrock (§18)
    llm_provider: LlmProvider = LlmProvider.BEDROCK
    aws_region: str = "us-east-1"
    bedrock_model_id: str = ""
    bedrock_embed_model_id: str = "amazon.titan-embed-text-v2:0"
    # Titan Text Embeddings V2 supports exactly these three output dimensions, which is what
    # `.env.example` documents. A free `int` accepts 768, and the failure then surfaces as a Bedrock
    # validation error on the first ingestion run rather than at startup.
    bedrock_embed_dimensions: Literal[256, 512, 1024] = 1024
    bedrock_guardrail_id: str = ""
    bedrock_max_tokens: Annotated[int, Field(ge=1, le=64_000)] = 1024
    bedrock_read_timeout_s: PositiveFloat = 8.0
    bedrock_max_retries: Annotated[int, Field(ge=0, le=10)] = 3

    # ---------------------------------------------------------------- retrieval (§18)
    retrieval_enabled: bool = True
    retrieval_lexical_k: PositiveInt = 20
    retrieval_semantic_k: PositiveInt = 20
    retrieval_rrf_k: PositiveInt = 60
    retrieval_context_chunks: PositiveInt = 5
    retrieval_min_score: Annotated[float, Field(ge=0.0, le=1.0)] = 0.02
    rerank_enabled: bool = False
    rerank_model_id: str = ""
    chunk_target_tokens: Annotated[int, Field(ge=64, le=8192)] = 500
    chunk_overlap_ratio: Annotated[float, Field(ge=0.0, le=0.5)] = 0.15

    # ---------------------------------------------------------------- caching (§13.1, task 16.2)
    # Short-lived cache of deterministic read models. Zero disables it (every get misses). Kept
    # small so a recompute is reflected within a minute even without an explicit invalidation.
    read_model_cache_ttl_s: Annotated[int, Field(ge=0)] = 60

    # ---------------------------------------------------------------- agents (§18)
    agent_cache_ttl_s: Annotated[int, Field(ge=0)] = 900
    agent_wave1_budget_s: PositiveFloat = 2.5
    agent_wave2_budget_s: PositiveFloat = 1.5
    agent_wave3_budget_s: PositiveFloat = 1.0
    qa_max_tool_calls: Annotated[int, Field(ge=1, le=20)] = 6
    prompt_registry_path: Path = Path("prompts/")

    # ---------------------------------------------------------------- graph (§18)
    graph_max_hops: Annotated[int, Field(ge=1, le=6)] = 3
    graph_node_cap: Annotated[int, Field(ge=1, le=10_000)] = 300

    # ------------------------------------------- configurable thresholds (§18, req 13.x)
    major_txn_absolute_threshold_cents: PositiveInt = 1_000_000
    major_txn_median_multiple: PositiveFloat = 10.0
    spend_anomaly_sigma: PositiveFloat = 2.0
    offer_cooling_off_days: Annotated[int, Field(ge=0, le=3650)] = 90

    # ------------------------------------------- signals feed (Phase 17, §18)
    # A large deposit opens a cross-sell window when a single credit lands above this floor; tuned
    # against the seeded HNW cohort so its outsized inflows surface and ordinary salary credits do
    # not. Integer cents end to end (design §1.1).
    signal_large_deposit_threshold_cents: PositiveInt = 5_000_000
    # Ranking weights (task 17.3). Score = severity_w·severity + value_w·value_at_stake_norm +
    # recency_w·recency. Kept as plain floats so an operator can retune the queue without a code
    # change; the ranker normalizes value-at-stake and recency to 0..1 before applying them.
    signal_rank_severity_weight: PositiveFloat = 1.0
    signal_rank_value_weight: PositiveFloat = 0.6
    signal_rank_recency_weight: PositiveFloat = 0.3
    # A dismissed signal stays suppressed for this window even if detection re-emits it, so an RM
    # who has triaged a signal is not shown it again the next day (task 17.3).
    signal_cooling_off_days: Annotated[int, Field(ge=0, le=3650)] = 30
    # The value-at-stake ceiling used to normalize the ranking term to 0..1. A signal worth more
    # than this is simply capped at the top of the value band rather than dominating the sum.
    signal_value_at_stake_cap_cents: PositiveInt = 100_000_000

    # ------------------------------------------- report exports (Phase 18, §18)
    # The bank name and a short tagline stamped on every generated report's cover. Branding is
    # per-report-definition too (task 18.1); these are the platform-wide defaults a definition
    # inherits when it sets none. Plain strings so an operator can rebrand without a code change.
    reports_brand_name: str = "Customer 360 Bank"
    reports_brand_tagline: str = "Relationship intelligence, on demand"
    # How many customers a single book/segment report will render before it stops, so a report over
    # a large book stays bounded in size and generation time. A customer report renders exactly one.
    reports_max_customers_per_run: Annotated[int, Field(ge=1, le=10_000)] = 100

    # ---------------------------------------------------------------- observability (§18)
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "c360-api"
    # Standard OpenTelemetry exporter selectors. `console` exists so the trace pipeline can be
    # exercised without a collector; `none` keeps the SDK active but exports nothing.
    otel_traces_exporter: Literal["otlp", "console", "none"] = "otlp"
    otel_metrics_exporter: Literal["otlp", "console", "none"] = "otlp"
    otel_metric_export_interval_ms: Annotated[int, Field(ge=1000, le=600_000)] = 60_000
    otel_traces_sampler_arg: Ratio = 1.0
    otel_capture_prompt_content: bool = False
    telemetry_customer_hash_salt: str = ""
    cost_price_table_path: Path = Path("config/bedrock_prices.json")
    # Default per-route HTTP latency budget for the RED budget-breach counter (design §13.3). A
    # request slower than this increments `c360.http.server.budget_breach` for its route; the SLO
    # dashboard (§13.5) reads the breach rate rather than a raw threshold alert.
    http_request_budget_ms: PositiveFloat = 3000.0

    # ---------------------------------------------------------------- evaluation (§18)
    eval_panel_per_cohort: PositiveInt = 3
    eval_groundedness_min: Ratio = 1.0
    eval_coverage_min: Ratio = 0.95
    eval_retrieval_recall_min: Ratio = 0.85
    eval_regression_tolerance_pts: Annotated[float, Field(ge=0.0, le=100.0)] = 1.0

    # ---------------------------------------------------------------- auth (§18, req 12.x)
    auth_provider: AuthProvider = AuthProvider.LOCAL
    jwt_private_key_path: Path | None = None
    jwt_access_ttl_s: Annotated[int, Field(ge=60, le=86_400)] = 900
    session_idle_timeout_s: Annotated[int, Field(ge=60, le=86_400)] = 1800
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    audit_retention_days: Annotated[int, Field(ge=1, le=3650)] = 365

    # ---------------------------------------------------------------- normalization
    @field_validator("bedrock_embed_dimensions", mode="before")
    @classmethod
    def _coerce_int_literal(cls, value: object) -> object:
        """Coerce the environment's string into an int before ``Literal`` membership is checked.

        Environment variables are always strings, and Pydantic does not coerce a ``str`` into an
        ``int`` ``Literal`` even in non-strict mode — ``'1024'`` is simply not one of the permitted
        values. Without this, constraining the field to Titan's three dimensions would reject the
        documented default.
        """
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return value

    @field_validator("jwt_private_key_path", mode="before")
    @classmethod
    def _blank_optional_path_is_none(cls, value: object) -> object:
        """Treat an empty environment value as "unset" rather than as the path ``""``.

        ``.env.example`` ships ``JWT_PRIVATE_KEY_PATH=`` with no value, and dotenv hands that over
        as the empty string. Pydantic then builds ``Path("")`` — which is not ``None``, so every
        ``is not None`` guard downstream treats it as a configured path that happens not to exist.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ---------------------------------------------------------------- derived paths
    def resolve(self, path: Path) -> Path:
        """Resolve a configured path against the project root when it is relative.

        Path configuration in §18 is written relative (``data/customer.db``). Resolving against
        the project root rather than the process working directory means the API behaves the same
        whether it is started from the repository root, from ``backend/``, or inside a container.
        """
        return path if path.is_absolute() else (PROJECT_ROOT / path)

    @property
    def customer_db_path(self) -> Path:
        return self.resolve(self.sqlite_db_path)

    @property
    def audit_db_path(self) -> Path:
        return self.resolve(self.sqlite_audit_db_path)

    @property
    def checkpoint_db_path(self) -> Path:
        return self.resolve(self.sqlite_checkpoint_db_path)

    @property
    def knowledge_db_path(self) -> Path:
        return self.resolve(self.sqlite_knowledge_db_path)

    @property
    def eval_db_path(self) -> Path:
        return self.resolve(self.sqlite_eval_db_path)

    @property
    def signals_db_path(self) -> Path:
        return self.resolve(self.sqlite_signals_db_path)

    @property
    def reports_db_path(self) -> Path:
        return self.resolve(self.sqlite_reports_db_path)

    @property
    def reports_output_path(self) -> Path:
        return self.resolve(self.reports_output_dir)

    @property
    def prompt_registry_dir(self) -> Path:
        return self.resolve(self.prompt_registry_path)

    @property
    def cost_price_table(self) -> Path:
        return self.resolve(self.cost_price_table_path)

    @property
    def is_production_like(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PROD)

    # ---------------------------------------------------------------- validation
    @model_validator(mode="after")
    def _validate_conditional_requirements(self) -> Self:
        """Fail fast on configuration that is required by another setting's value.

        Cross-field rules cannot be expressed as field constraints, and getting them wrong is
        exactly the class of problem that otherwise appears as a 500 on the first Bedrock call.
        """
        missing: list[str] = []

        if self.llm_provider is LlmProvider.BEDROCK and not self.bedrock_model_id.strip():
            missing.append("BEDROCK_MODEL_ID is required when LLM_PROVIDER=bedrock")

        if self.llm_provider is LlmProvider.BEDROCK and not self.aws_region.strip():
            missing.append("AWS_REGION is required when LLM_PROVIDER=bedrock")

        if self.rerank_enabled and not self.rerank_model_id.strip():
            missing.append("RERANK_MODEL_ID is required when RERANK_ENABLED=true")

        if self.auth_provider is AuthProvider.OIDC:
            if not self.oidc_issuer.strip():
                missing.append("OIDC_ISSUER is required when AUTH_PROVIDER=oidc")
            if not self.oidc_client_id.strip():
                missing.append("OIDC_CLIENT_ID is required when AUTH_PROVIDER=oidc")

        if self.otel_enabled and not self.otel_exporter_otlp_endpoint.strip():
            missing.append("OTEL_EXPORTER_OTLP_ENDPOINT is required when OTEL_ENABLED=true")

        # Only checked when a path is actually configured. `_blank_optional_path_is_none` is what
        # makes that distinction reliable; without it an unset variable arrives as `Path("")` and
        # this check fails for every deployment that has not generated a key yet.
        if self.jwt_private_key_path is not None:
            key_path = self.resolve(self.jwt_private_key_path)
            if not key_path.is_file():
                missing.append(f"JWT_PRIVATE_KEY_PATH does not point at a file: {key_path}")

        if self.retrieval_context_chunks > max(self.retrieval_lexical_k, self.retrieval_semantic_k):
            missing.append(
                "RETRIEVAL_CONTEXT_CHUNKS cannot exceed the larger of "
                "RETRIEVAL_LEXICAL_K and RETRIEVAL_SEMANTIC_K"
            )

        # Prompt content capture is a debugging affordance, never a deployment option.
        if self.otel_capture_prompt_content and self.is_production_like:
            missing.append(
                "OTEL_CAPTURE_PROMPT_CONTENT must be false when ENVIRONMENT is staging or prod"
            )

        if missing:
            raise ConfigurationError(
                "Invalid configuration:\n  - " + "\n  - ".join(missing),
            )
        return self

    # ---------------------------------------------------------------- startup advisories
    def startup_warnings(self) -> list[str]:
        """Non-fatal configuration concerns, emitted once at startup.

        Returned rather than logged so the caller controls ordering and the list is directly
        assertable in tests.
        """
        warnings: list[str] = []

        if self.otel_capture_prompt_content:
            warnings.append(
                "OTEL_CAPTURE_PROMPT_CONTENT is enabled: prompt and completion content will be "
                "attached to spans. This is for local debugging only — prompt content belongs in "
                "the access-controlled audit log, not in a telemetry backend (design §13.4).",
            )

        if not self.telemetry_customer_hash_salt.strip():
            warnings.append(
                "TELEMETRY_CUSTOMER_HASH_SALT is empty: pseudonymous customer references in "
                "traces are disabled (requirement 18.9).",
            )
        elif len(self.telemetry_customer_hash_salt) < MIN_HASH_SALT_LENGTH:
            warnings.append(
                "TELEMETRY_CUSTOMER_HASH_SALT is shorter than 16 characters, which weakens the "
                "pseudonymous customer reference against brute-force reversal.",
            )

        if self.llm_provider is LlmProvider.MOCK:
            warnings.append(
                "LLM_PROVIDER=mock: agent narratives are deterministic templates, not model "
                "output. Correct for CI and evaluation; not a production configuration.",
            )

        if not self.otel_enabled:
            warnings.append(
                "OTEL_ENABLED=false: no traces or metrics will be exported.",
            )

        return warnings


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached because settings are immutable for the lifetime of the process, and because both
    FastAPI dependencies and module-level consumers need the same instance. Tests clear the cache
    via :func:`reset_settings_cache`.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings instance. Test-support only."""
    get_settings.cache_clear()
