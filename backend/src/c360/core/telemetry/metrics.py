"""The platform metric inventory (task 10.2, design §13.3, requirements 18.4-18.6).

Every metric group from design §13.3 that is not already owned by a subsystem lives here:
HTTP RED, agent outcomes, model tokens/cost, Q&A behaviour, database pool, graph traversal and the
security counters. The two subsystems that predate this module keep their own instruments —
retrieval in :mod:`c360.knowledge.metrics` and the audit queue/fail-closed pair in
:mod:`c360.security.audit` — because they were built alongside their code and the design gate for
those phases already asserts them; duplicating them here would double-count.

Three rules hold throughout:

* **Lazy instruments.** Nothing is created until :meth:`PlatformMetrics.ensure` runs, so a process
  with telemetry disabled — every CLI entry point — pays nothing. This mirrors the audit and
  retrieval subsystems.
* **Every label passes the allowlist.** :func:`c360.core.telemetry.labels.scrub_labels` runs on
  every attribute mapping before ``add``/``record``, so no instrument here can attach a
  ``customer_id`` or a monetary value even if a caller supplies one (design §13.4).
* **No money, ever.** Balances and amounts are customer data regardless of transport. The one
  cost-shaped metric is *estimated* micro-USD derived from token counts, not a real balance.
"""

from __future__ import annotations

from typing import Final

from opentelemetry.metrics import Counter, Histogram

from c360.core.telemetry.bootstrap import get_meter
from c360.core.telemetry.cost import CostEstimator
from c360.core.telemetry.labels import scrub_labels

_METER_NAME: Final = "c360.platform"


class PlatformMetrics:
    """The process-wide metric inventory. Built once at startup, shared by every emitter."""

    __slots__ = (
        "_agent_citations",
        "_agent_duration",
        "_agent_outcome",
        "_breaker_state",
        "_claim_rejections",
        "_cost",
        "_db_pool_wait",
        "_db_query_duration",
        "_db_sqlite_busy",
        "_denied_access",
        "_ensured",
        "_estimator",
        "_graph_hops",
        "_graph_nodes",
        "_graph_truncated",
        "_http_duration",
        "_http_errors",
        "_http_requests",
        "_masking_applied",
        "_model_duration",
        "_model_throttled",
        "_model_tokens",
        "_qa_clarifications",
        "_qa_refusals",
        "_qa_tool_calls",
        "_route_budget_breach",
    )

    def __init__(self, estimator: CostEstimator) -> None:
        self._ensured = False
        self._estimator = estimator
        # Instruments are typed as objects until ``ensure`` builds them; the public methods guard on
        # ``_ensured`` so a pre-startup call is a silent no-op rather than an attribute error.

    # ------------------------------------------------------------------ lifecycle
    def ensure(self) -> None:
        """Create every instrument. Idempotent; safe to call from startup and from tests."""
        if self._ensured:
            return
        meter = get_meter(_METER_NAME)

        # -------------------------------------------------------------- HTTP (RED)
        self._http_duration: Histogram = meter.create_histogram(
            "c360.http.server.request.duration",
            unit="ms",
            description="HTTP server request duration by route, method and status.",
        )
        self._http_requests: Counter = meter.create_counter(
            "c360.http.server.requests", description="HTTP requests handled."
        )
        self._http_errors: Counter = meter.create_counter(
            "c360.http.server.errors", description="HTTP responses with a 5xx status."
        )
        self._route_budget_breach: Counter = meter.create_counter(
            "c360.http.server.budget_breach",
            description="Requests exceeding the per-route latency budget.",
        )

        # -------------------------------------------------------------- Agents
        self._agent_duration = meter.create_histogram(
            "c360.agent.duration",
            unit="ms",
            description="Agent node duration by agent.",
        )
        self._agent_outcome = meter.create_counter(
            "c360.agent.outcome",
            description="Agent completions by outcome (success/timeout/degraded/cache_hit).",
        )
        self._claim_rejections = meter.create_counter(
            "c360.agent.claim_rejections",
            description="Claim-validator rejections of a generated figure.",
        )
        self._agent_citations = meter.create_histogram(
            "c360.agent.citations",
            description="Citations attached to an agent output.",
        )

        # -------------------------------------------------------------- Model
        self._model_duration = meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="ms",
            description="Model call duration by model and agent.",
        )
        self._model_tokens = meter.create_counter(
            "gen_ai.client.token.usage",
            description="Model tokens consumed, labelled input/output by model and agent.",
        )
        self._model_throttled = meter.create_counter(
            "c360.model.throttled",
            description="Model calls that hit a throttling response.",
        )
        self._cost = meter.create_counter(
            "c360.model.estimated_cost.micro_usd",
            unit="uUSD",
            description="Estimated model spend in micro-USD, derived from tokens x price table.",
        )

        # -------------------------------------------------------------- Q&A
        self._qa_tool_calls = meter.create_histogram(
            "c360.qa.tool_calls",
            description="Tool calls made to answer one Q&A question.",
        )
        self._qa_refusals = meter.create_counter(
            "c360.qa.refusals", description="Q&A refusals, labelled by reason."
        )
        self._qa_clarifications = meter.create_counter(
            "c360.qa.clarifications", description="Q&A clarifying questions asked."
        )

        # -------------------------------------------------------------- Database
        self._db_pool_wait = meter.create_histogram(
            "c360.db.pool.checkout_wait",
            unit="ms",
            description="Connection-pool checkout wait time.",
        )
        self._db_query_duration = meter.create_histogram(
            "c360.db.query.duration",
            unit="ms",
            description="Query duration by statement id.",
        )
        self._db_sqlite_busy = meter.create_counter(
            "c360.db.sqlite_busy",
            description="SQLITE_BUSY retries.",
        )

        # -------------------------------------------------------------- Graph
        self._graph_hops = meter.create_histogram(
            "c360.graph.hops", description="Hops traversed in a graph query."
        )
        self._graph_nodes = meter.create_histogram(
            "c360.graph.nodes_visited", description="Nodes visited in a graph query."
        )
        self._graph_truncated = meter.create_counter(
            "c360.graph.truncated",
            description="Graph traversals truncated by the hop or node cap.",
        )

        # -------------------------------------------------------------- Security
        self._denied_access = meter.create_counter(
            "c360.security.denied_access",
            description="Entitlement denials, labelled by role.",
        )
        self._masking_applied = meter.create_counter(
            "c360.security.masking_applied",
            description="Field-masking applications, labelled by field group.",
        )
        self._breaker_state = meter.create_counter(
            "c360.security.breaker_transition",
            description="Circuit-breaker transitions, labelled by breaker and state.",
        )

        self._ensured = True

    # ------------------------------------------------------------------ HTTP (RED)
    def record_http(
        self, *, method: str, route: str, status_code: int, duration_ms: float, over_budget: bool
    ) -> None:
        if not self._ensured:
            return
        labels = scrub_labels(
            {
                "http.request.method": method,
                "route": route,
                "http.response.status_code": status_code,
            }
        )
        self._http_duration.record(duration_ms, labels)
        self._http_requests.add(1, labels)
        if status_code >= 500:  # noqa: PLR2004 - HTTP server-error class
            self._http_errors.add(1, labels)
        if over_budget:
            self._route_budget_breach.add(1, {"route": route})

    # ------------------------------------------------------------------ Agents
    def record_agent(self, *, agent: str, outcome: str, duration_ms: float, citations: int) -> None:
        if not self._ensured:
            return
        self._agent_duration.record(duration_ms, scrub_labels({"agent": agent}))
        self._agent_outcome.add(1, scrub_labels({"agent": agent, "outcome": outcome}))
        self._agent_citations.record(citations, scrub_labels({"agent": agent}))

    def record_claim_rejection(self, *, agent: str) -> None:
        if not self._ensured:
            return
        self._claim_rejections.add(1, scrub_labels({"agent": agent}))

    # ------------------------------------------------------------------ Model
    def record_model_call(
        self,
        *,
        model: str,
        agent: str,
        duration_ms: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if not self._ensured:
            return
        base = scrub_labels({"model": model, "agent": agent})
        self._model_duration.record(duration_ms, base)
        self._model_tokens.add(input_tokens, {**base, "outcome": "input"})
        self._model_tokens.add(output_tokens, {**base, "outcome": "output"})
        cost = self._estimator.micro_usd(
            model, input_tokens=input_tokens, output_tokens=output_tokens
        )
        if cost:
            self._cost.add(cost, base)

    def record_model_throttled(self, *, model: str) -> None:
        if not self._ensured:
            return
        self._model_throttled.add(1, scrub_labels({"model": model}))

    # ------------------------------------------------------------------ Q&A
    def record_qa(self, *, tool_calls: int, refused_reason: str | None, clarified: bool) -> None:
        if not self._ensured:
            return
        self._qa_tool_calls.record(tool_calls)
        if refused_reason is not None:
            self._qa_refusals.add(1, scrub_labels({"reason": refused_reason}))
        if clarified:
            self._qa_clarifications.add(1)

    # ------------------------------------------------------------------ Database
    def record_pool_wait(self, *, wait_ms: float) -> None:
        if not self._ensured:
            return
        self._db_pool_wait.record(wait_ms)

    def record_query(self, *, statement_id: str, duration_ms: float) -> None:
        if not self._ensured:
            return
        self._db_query_duration.record(duration_ms, scrub_labels({"statement_id": statement_id}))

    def record_sqlite_busy(self) -> None:
        if not self._ensured:
            return
        self._db_sqlite_busy.add(1)

    # ------------------------------------------------------------------ Graph
    def record_graph(self, *, hops: int, nodes_visited: int, truncated: bool) -> None:
        if not self._ensured:
            return
        self._graph_hops.record(hops)
        self._graph_nodes.record(nodes_visited)
        if truncated:
            self._graph_truncated.add(1)

    # ------------------------------------------------------------------ Security
    def record_denied_access(self, *, role: str) -> None:
        if not self._ensured:
            return
        self._denied_access.add(1, scrub_labels({"role": role}))

    def record_masking(self, *, field_group: str) -> None:
        if not self._ensured:
            return
        self._masking_applied.add(1, scrub_labels({"field_group": field_group}))

    def record_breaker_transition(self, *, breaker: str, state: str) -> None:
        if not self._ensured:
            return
        self._breaker_state.add(1, scrub_labels({"breaker": breaker, "state": state}))


class _MetricsState:
    """Holder for the process-wide metrics facade, avoiding a module ``global``."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: PlatformMetrics | None = None


_state = _MetricsState()


def setup_metrics(estimator: CostEstimator) -> PlatformMetrics:
    """Build and install the process-wide :class:`PlatformMetrics`. Idempotent."""
    if _state.value is None:
        metrics = PlatformMetrics(estimator)
        metrics.ensure()
        _state.value = metrics
    return _state.value


def get_metrics() -> PlatformMetrics | None:
    """Return the installed metrics facade, or ``None`` before :func:`setup_metrics` has run."""
    return _state.value


def breaker_transition_hook(breaker: str, state: object) -> None:
    """A :class:`~c360.agents.breaker.CircuitBreaker` transition hook that records the metric.

    Passed into every breaker at construction so an open/close transition increments
    ``c360.security.breaker_transition`` (task 10.4). Kept here rather than in the breaker so the
    breaker stays dependency-free. A no-op before metrics are installed (CLI paths).
    """
    metrics = _state.value
    if metrics is not None:
        metrics.record_breaker_transition(breaker=breaker, state=str(state))


def reset_metrics() -> None:
    """Drop the installed facade. Test-support only."""
    _state.value = None


__all__ = [
    "PlatformMetrics",
    "get_metrics",
    "reset_metrics",
    "setup_metrics",
]
