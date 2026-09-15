"""The evaluation harness (task 11.2, design §14.2).

The harness executes the agents and Q&A over the panel *through the same entry points the API uses*
-- :class:`~c360.agents.runtime.AgentRuntime` and its :class:`~c360.agents.graph.DashboardGraph` /
:class:`~c360.agents.qa_graph.QaGraph` -- so what is scored is the real system, not a copy
of it. This is design §14.2's requirement that the harness run "through the same entry points the
API
uses"; it is what makes a passing eval a statement about the deployed behaviour.

Determinism and isolation
-------------------------

A run seeds a throwaway customer database from the *same seed and count as the ground truth*, so
panel membership and every fact line up with the exported labels (design §14.2: "generated from the
same seed as the customer data so panel membership is stable"). The database lives under a
caller-provided directory (a temp dir in CI, a stable path when a developer wants to inspect it) and
services are built read-only over it exactly as the app builds them. The agent output cache is
disabled for the run (a fresh :class:`DashboardGraph` with ``cache=None`` is zero-TTL) so every card
is genuinely regenerated and a stale narrative can never mask a regression.

What it collects
----------------

For each panel customer the harness runs the seven dashboard agents once and records every
:class:`~c360.agents.result.AgentResult` together with the :class:`~c360.tools.facts.FactTable` the
agent was grounded on -- the scorers need the facts to check groundedness, provenance and coverage.
Q&A and adversarial execution are driven separately (they need per-question inputs), but they reuse
the same runtime and services this harness builds, exposed through :meth:`ask` and the accessors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from c360.agents.runtime import build_agent_runtime
from c360.data.recompute import recompute
from c360.eval.principals import HARNESS_ROLE, principal_for_role
from c360.generator.context import DEFAULT_AS_OF
from c360.generator.pipeline import seed as seed_database

if TYPE_CHECKING:
    from datetime import date

    from c360.agents.graph import DashboardGraph
    from c360.agents.qa_graph import QaState
    from c360.agents.result import AgentResult
    from c360.agents.runtime import AgentRuntime
    from c360.api.services import Services
    from c360.core.config import Settings
    from c360.eval.ground_truth import GroundTruth
    from c360.security.model import Principal
    from c360.tools.facts import FactTable


@dataclass(frozen=True, slots=True)
class AgentRun:
    """One agent's output plus the facts it was grounded on, for one customer (task 11.2).

    The scorers read ``result`` for the narrative, citations, confidence and degraded flag, and
    ``facts`` to resolve the citations and check numeric provenance. Keeping the two together is why
    the harness fetches the load-context fact tables rather than only the final results.
    """

    customer_id: str
    agent: str
    result: AgentResult
    facts: FactTable

    @property
    def narrative(self) -> str:
        """The agent's narrative prose.

        ``AgentResult.outputs`` is typed as a bare ``BaseModel`` on the shared result type, but
        every agent output extends ``_Output`` which carries ``narrative``. Reading it through one
        accessor keeps the ``getattr`` in a single place rather than scattered through the scorers.
        """
        return str(getattr(self.result.outputs, "narrative", ""))


@dataclass(frozen=True, slots=True)
class HarnessOutput:
    """Everything a run's dashboard pass produced, indexed for the scorers."""

    #: All agent runs, in (customer, agent) order.
    agent_runs: tuple[AgentRun, ...] = ()

    def for_agent(self, agent: str) -> tuple[AgentRun, ...]:
        return tuple(run for run in self.agent_runs if run.agent == agent)

    def for_customer(self, customer_id: str) -> tuple[AgentRun, ...]:
        return tuple(run for run in self.agent_runs if run.customer_id == customer_id)


class EvalHarness:
    """Seeds a panel database, builds the real runtime, and runs agents/Q&A over the panel."""

    __slots__ = ("_as_of", "_ground_truth", "_runtime", "_services", "_settings", "_workdir")

    def __init__(
        self,
        settings: Settings,
        ground_truth: GroundTruth,
        *,
        workdir: Path,
        as_of: date = DEFAULT_AS_OF,
    ) -> None:
        self._settings = settings
        self._ground_truth = ground_truth
        self._workdir = workdir
        self._as_of = as_of
        self._runtime: AgentRuntime | None = None
        self._services: Services | None = None

    # ---------------------------------------------------------------- setup
    def prepare(self) -> None:
        """Seed the panel database from the ground-truth seed and build services + runtime.

        Idempotent within a harness instance: calling it twice reuses the built runtime. The
        database is seeded with ``force=True`` because the workdir is the harness's own throwaway
        space, so an earlier run's file is expected and replaced.
        """
        if self._services is not None:
            return
        db_path = self._workdir / "customer.db"
        seed_database(
            database=db_path,
            count=self._ground_truth.count,
            seed=self._ground_truth.seed,
            as_of=self._as_of,
            force=True,
        )
        recompute(db_path)
        # Point settings at the seeded database so the app's own service builder opens it. The
        # writable report store (Phase 18) is routed into the harness workdir too, so an eval run
        # builds its report artifacts in the throwaway space rather than the repo's data/ directory.
        # The writable stores (Q&A checkpoints and the Phase 18 report store) are routed into the
        # harness workdir too, so each harness run gets its own isolated files rather than sharing
        # the repo's data/ directory across runs — a shared checkpoints.db is what otherwise locks
        # under back-to-back Q&A eval runs in one process.
        run_settings = self._settings.model_copy(
            update={
                "sqlite_db_path": db_path,
                "sqlite_checkpoint_db_path": self._workdir / "checkpoints.db",
                "sqlite_reports_db_path": self._workdir / "reports.db",
                "reports_output_dir": self._workdir / "reports",
            }
        )
        from c360.api.services import build_services  # noqa: PLC0415 - avoid import cycle at load

        self._services = build_services(run_settings)
        self._runtime = build_agent_runtime(run_settings)

    @property
    def services(self) -> Services:
        self._require_prepared()
        assert self._services is not None  # noqa: S101 - guarded by _require_prepared
        return self._services

    @property
    def runtime(self) -> AgentRuntime:
        self._require_prepared()
        assert self._runtime is not None  # noqa: S101 - guarded by _require_prepared
        return self._runtime

    def prompt_versions(self) -> dict[str, str]:
        """Agent name → the prompt version the run exercised (for the run record, design §14.6)."""
        from c360.agents.specs import AGENT_SPECS  # noqa: PLC0415

        versions: dict[str, str] = {}
        for name in AGENT_SPECS:
            prompt = self.runtime.prompts.for_agent(name)
            versions[name] = prompt.version
        return versions

    @property
    def model_id(self) -> str:
        return self.runtime.provider.model_id

    # ---------------------------------------------------------------- dashboard pass
    async def run_dashboard(self, principal: Principal | None = None) -> HarnessOutput:
        """Run the seven dashboard agents over every panel customer once (design §14.2).

        Uses a fresh, cache-disabled :class:`DashboardGraph` so each card is regenerated. The
        principal defaults to the RM (:data:`HARNESS_ROLE`) -- the widest field set short of Risk --
        so a groundedness or coverage failure is measured on the agent, not masked away by a
        restrictive policy. The entitlement-safety scorer supplies its own per-role principals.
        """
        output, _elapsed = await self.run_dashboard_timed(principal)
        return output

    async def run_dashboard_timed(
        self, principal: Principal | None = None
    ) -> tuple[HarnessOutput, dict[str, float]]:
        """Run the dashboard over the panel, returning the output and per-customer wall-clock (ms).

        The timing is the full-graph latency per customer -- every agent for that customer through
        the
        real wave topology -- which is the number the design §13 dashboard budget is expressed in
        (a 360 view is judged as a whole, not agent by agent). The performance scorer derives
        p50/p95
        from these. A fresh, cache-disabled graph is used so a warm cache never flatters a timing.
        """
        self._require_prepared()
        actor = principal or principal_for_role(HARNESS_ROLE)
        graph = self._fresh_graph()
        runs: list[AgentRun] = []
        elapsed_ms: dict[str, float] = {}
        for customer_id in self._ground_truth.panel:
            start = perf_counter()
            state = await graph.arun(actor, customer_id)
            elapsed_ms[customer_id] = (perf_counter() - start) * 1000.0
            facts_by_agent = state.get("facts", {})
            for agent, result in state.get("agent_outputs", {}).items():
                runs.append(
                    AgentRun(
                        customer_id=customer_id,
                        agent=agent,
                        result=result,
                        facts=facts_by_agent.get(agent, _empty_fact_table()),
                    )
                )
        return HarnessOutput(agent_runs=tuple(runs)), elapsed_ms

    def _fresh_graph(self) -> DashboardGraph:
        """A dashboard graph with caching disabled, bound to this run's services (task 11.2)."""
        from c360.agents.graph import DashboardGraph  # noqa: PLC0415

        return DashboardGraph(
            registry=self.runtime.registry,
            services=self.services,
            provider=self.runtime.provider,
            prompts=self.runtime.prompts,
            cache=None,
            audit_sink=None,
            breaker=self.runtime.breaker,
        )

    # ---------------------------------------------------------------- Q&A pass
    async def ask(self, principal: Principal, customer_id: str, question: str) -> QaState:
        """Answer one question through the real Q&A graph (task 11.2, 11.7).

        Each call gets a unique, single-use ``thread_id``, so every question is an isolated
        conversation and answers cannot leak between panel items. A fresh id per call is required
        rather than omitting one: the runtime's Q&A graph is compiled with the memory checkpointer,
        which mandates a thread key -- a distinct id per question is the eval equivalent of a
        stateless run.
        """
        self._require_prepared()
        qa_graph = await self.runtime.qa_graph_for(self.services)
        thread_id = f"eval-{uuid4().hex}"
        return await qa_graph.arun(principal, customer_id, question, thread_id=thread_id)

    # ---------------------------------------------------------------- teardown
    async def aclose_qa(self) -> None:
        """Close the Q&A memory's aiosqlite connection, if the Q&A path was used.

        The checkpointer binds an aiosqlite connection to the event loop it was created on, so it
        must be closed on that same loop before the loop ends -- otherwise the connection's
        background thread keeps the loop alive and a caller that does its Q&A inside one
        ``asyncio.run`` would hang on teardown. Safe to call when Q&A was never used.
        """
        if self._runtime is not None:
            await self._runtime.memory.aclose()

    def dispose(self) -> None:
        """Release the customer engine's connection pool."""
        if self._services is not None:
            self._services.engine.dispose()
            if self._services.knowledge_engine is not None:
                self._services.knowledge_engine.dispose()
            self._services = None

    # ---------------------------------------------------------------- internals
    def _require_prepared(self) -> None:
        if self._services is None or self._runtime is None:
            raise RuntimeError("harness.prepare() must be called before running")


def _empty_fact_table() -> FactTable:
    from c360.tools.facts import FactTable  # noqa: PLC0415

    return FactTable()


__all__ = ["AgentRun", "EvalHarness", "HarnessOutput"]
