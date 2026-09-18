"""Phase 6 tool-registry tests (tasks 6.1-6.5).

Exercised against the generated, recomputed Phase 5 dataset so the tools call the same services over
the same data the REST tests use. The tests assert the four things the Phase 6 gate names:

* facts are wrapped with provenance and stable numbered ids (6.1);
* a tool call goes through the same authorization, masking and graph redaction as REST (6.2, 6.5);
* the exported schema is a well-formed Bedrock/OpenAI function-tool object (6.3);
* a tool execution emits a ``gen_ai.execute_tool`` span carrying the name and outcome and *no*
  argument values (6.4).
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine

from c360.api.services import Services
from c360.core.config import Settings
from c360.data.repositories import build_repositories
from c360.domain.enums import CustomerSegment, RiskBand
from c360.security.entitlement import BookScope, SegmentScope
from c360.security.errors import EntitlementError
from c360.security.model import Principal, Role
from c360.security.serializer import mask_model
from c360.services.aggregator import C360Aggregator
from c360.services.customer import CustomerService, RecentlyViewedTracker
from c360.services.financial import FinancialService
from c360.services.journey import JourneyService
from c360.services.offer import OfferService
from c360.services.relationship import RelationshipService
from c360.services.risk import RiskService
from c360.tools import build_tool_registry, tool_schemas
from c360.tools.customer_tools import (
    AggregateArgs,
    AggregateMetric,
    ToolResult,
    _FinancialProfileResponse,
)
from c360.tools.facts import FactTable
from c360.tools.registry import ToolContext, ToolRegistry
from tests.phase5_fixtures import all_customer_ids, principal_for, query_one

# ==================================================================== fixtures / builders


def _services(engine: Engine, settings: Settings) -> Services:
    """Construct the full service container over an already-open engine.

    Mirrors :func:`c360.api.services.build_services` but takes the engine directly, so a test runs
    against the shared read-only ``phase5_engine`` without re-opening the database.
    """
    repos = build_repositories(engine)
    recent = RecentlyViewedTracker()
    customer = CustomerService(repos.customer, recently_viewed=recent)
    financial = FinancialService(repos.financial, spend_anomaly_sigma=settings.spend_anomaly_sigma)
    relationship = RelationshipService(
        repos.relationship,
        repos.graph,
        repos.customer,
        repos.financial,
        max_hops=settings.graph_max_hops,
        node_cap=settings.graph_node_cap,
    )
    risk = RiskService(repos.risk)
    offer = OfferService(repos.offer, cooling_off_days=settings.offer_cooling_off_days)
    journey = JourneyService(
        repos.journey,
        repos.relationship,
        major_txn_absolute_threshold_cents=settings.major_txn_absolute_threshold_cents,
        major_txn_median_multiple=settings.major_txn_median_multiple,
    )
    aggregator = C360Aggregator(
        customer=customer,
        financial=financial,
        relationship=relationship,
        risk=risk,
        offer=offer,
        journey=journey,
        max_workers=settings.sqlite_read_pool_size,
    )
    return Services(
        engine=engine,
        customer=customer,
        financial=financial,
        relationship=relationship,
        risk=risk,
        offer=offer,
        journey=journey,
        aggregator=aggregator,
        # The tool tests exercise the customer tools only; knowledge_search is not called here, so
        # the knowledge service can be absent (as it is on a deployment without an ingested KB).
        knowledge=None,
        knowledge_engine=None,
    )


@pytest.fixture
def services(phase5_engine: Engine, settings: Settings) -> Services:
    return _services(phase5_engine, settings)


@pytest.fixture
def registry() -> ToolRegistry:
    return build_tool_registry()


def _context(services: Services, principal: Principal) -> ToolContext:
    return ToolContext(principal=principal, services=services)


def _run(
    registry: ToolRegistry,
    name: str,
    context: ToolContext,
    arguments: Any,
) -> ToolResult:
    """Execute a tool and narrow the ``BaseModel`` return to :class:`ToolResult` for the assertions.

    Every registered tool returns a :class:`ToolResult`; the registry types ``execute`` as the base
    ``BaseModel`` because it dispatches by name. The assert documents and enforces the contract.
    """
    result = registry.execute(name, context, arguments)
    assert isinstance(result, ToolResult)
    return result


def _data_dict(result: ToolResult) -> dict[str, Any]:
    """Narrow a result's ``dict | list`` payload to the dict these tools return."""
    assert isinstance(result.data, dict)
    return result.data


def _first_customer(phase5_engine: Engine) -> str:
    return all_customer_ids(phase5_engine)[0]


def _customer_name(engine: Engine, customer_id: str) -> tuple[str, str]:
    """The ``(customer_id, customer_name)`` for one customer, for a name-based search test."""
    row = query_one(
        engine,
        "SELECT customer_id, customer_name FROM customer WHERE customer_id = :id",
        {"id": customer_id},
    )
    return str(row[0]), str(row[1])


def _first_customer_and_name(engine: Engine) -> tuple[str, str]:
    return _customer_name(engine, _first_customer(engine))


# ==================================================================== 6.1 fact-wrapping


class TestFactWrapping:
    def test_facts_carry_the_provenance_quadruple(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "financial_profile", context, {"customer_id": customer_id})

        assert len(result.facts) > 0
        for fact in result.facts.facts:
            assert fact.entity_type
            assert fact.entity_id
            assert fact.field
            # as_of is present because a financial profile is a SourcedModel.
            assert fact.as_of is not None

    def test_fact_ids_are_stable_and_sequential(self) -> None:
        builder = FactTable.builder()
        first = builder.add(entity_type="customer", entity_id="C1", field="a", value=1)
        second = builder.add(entity_type="customer", entity_id="C1", field="b", value=2)
        table = builder.build()

        assert first.fact_id == "F1"
        assert second.fact_id == "F2"
        assert table.by_id("F1") is first
        assert table.by_id("F2") is second
        assert table.by_id("F3") is None

    def test_money_facts_stay_integer_cents(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "financial_profile", context, {"customer_id": customer_id})

        money_facts = [f for f in result.facts.facts if f.field.endswith("_cents")]
        assert money_facts, "a financial profile should yield at least one monetary fact"
        assert all(isinstance(f.value, int) for f in money_facts)


# ==================================================================== 6.2 tools over services


# The tools keyed only on a customer id. Exercised end to end so every tool's service call, masking
# and fact-wrapping path runs at least once for an entitled role.
_SINGLE_CUSTOMER_TOOLS = (
    "profile",
    "contact",
    "holdings",
    "financial_profile",
    "credit_profile",
    "risk_profile",
    "expense_analytics",
    "transactions_query",
    "offers",
    "life_events",
    "journey_timeline",
    "household",
    "relationships",
    "graph_neighborhood",
)


class TestToolsOverServices:
    @pytest.mark.parametrize("tool_name", _SINGLE_CUSTOMER_TOOLS)
    def test_each_single_customer_tool_executes_and_returns_a_result(
        self,
        services: Services,
        registry: ToolRegistry,
        phase5_engine: Engine,
        tool_name: str,
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, tool_name, context, {"customer_id": customer_id})

        assert result.entity_id == customer_id
        assert isinstance(result.data, (dict, list))

    @pytest.mark.parametrize("metric", list(AggregateMetric))
    def test_every_whitelisted_aggregate_computes(
        self,
        services: Services,
        registry: ToolRegistry,
        phase5_engine: Engine,
        metric: AggregateMetric,
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(
            registry,
            "aggregate",
            context,
            AggregateArgs(customer_id=customer_id, metric=metric),
        )

        data = _data_dict(result)
        assert data["metric"] == str(metric)
        # Value is an int or None (None only when the customer has no household / profile).
        assert data["value"] is None or isinstance(data["value"], int)

    def test_graph_path_between_two_entitled_customers(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        ids = all_customer_ids(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(
            registry,
            "graph_path",
            context,
            {"source_customer_id": ids[0], "target_customer_id": ids[0]},
        )

        data = _data_dict(result)
        # A path from a customer to themselves is trivially the single node, length zero.
        assert data["length"] == 0

    def test_every_design_tool_is_registered(self, registry: ToolRegistry) -> None:
        expected = {
            # Phase 9 (task 9.6): the cross-customer resolver — how the search-landing Ask AI path
            # finds which customer a question is about before reading their details.
            "customer_search",
            # Book-level cohort listing and the pitch composer (cross-customer Q&A + "prepare a
            # pitch"): the two tools that let Ask AI answer questions about a *set* of customers and
            # assemble grounded talking points, not just single-customer reads.
            "customer_cohort",
            "pitch",
            "profile",
            "contact",
            "holdings",
            "financial_profile",
            "credit_profile",
            "risk_profile",
            "expense_analytics",
            "transactions_query",
            "offers",
            "life_events",
            "journey_timeline",
            "household",
            "relationships",
            "graph_neighborhood",
            "graph_path",
            "aggregate",
            # Phase 7 (design §9.6, §10.3): knowledge_search joins the same registry as the
            # customer tools so it inherits the entitlement gate, audit trail and tracing.
            "knowledge_search",
        }
        assert set(registry.names()) == expected

    def test_profile_tool_returns_the_same_data_the_service_would(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "profile", context, {"customer_id": customer_id})

        assert result.entity_id == customer_id
        assert _data_dict(result)["profile"]["customer_id"] == customer_id

    def test_customer_search_tool_resolves_a_query_to_entitled_matches(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """The cross-customer resolver (task 9.6) returns display-only, entitled matches."""
        customer_id, name = _first_customer_and_name(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "customer_search", context, {"query": name})

        data = _data_dict(result)
        matches = data["matches"]
        assert isinstance(matches, list)
        # The searched-for customer is among the resolved matches...
        assert any(match["customer_id"] == customer_id for match in matches)
        # ...and the hits are display-only: id, name, segment, city — no financial figures.
        for match in matches:
            assert set(match) == {"customer_id", "customer_name", "customer_segment", "city"}
        # A resolver hit is a picker, not citable data, so it carries no facts.
        assert result.facts.facts == ()

    def test_customer_search_tool_is_scoped_to_the_entitled_book(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """A restricted book never resolves a customer outside it (requirement 3.3, 12.3)."""
        ids = all_customer_ids(phase5_engine)
        outside_id, outside_name = _customer_name(phase5_engine, ids[-1])
        # A book that contains only the first customer, never the one we search for by name.
        principal = principal_for(Role.RM, BookScope(customer_ids=frozenset(ids[:1])))
        context = _context(services, principal)

        result = _run(registry, "customer_search", context, {"query": outside_name})

        matches = _data_dict(result)["matches"]
        assert all(match["customer_id"] != outside_id for match in matches)

    def test_aggregate_rejects_a_metric_outside_the_whitelist(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        with pytest.raises((ValueError, Exception)):
            # An unknown metric fails Pydantic validation before the handler runs.
            registry.execute(
                "aggregate",
                context,
                {"customer_id": customer_id, "metric": "arbitrary_sum_of_secret_column"},
            )

    def test_aggregate_computes_a_whitelisted_metric(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(
            registry,
            "aggregate",
            context,
            AggregateArgs(customer_id=customer_id, metric=AggregateMetric.OPEN_ACCOUNT_COUNT),
        )

        data = _data_dict(result)
        assert data["metric"] == "open_account_count"
        assert isinstance(data["value"], int)


# ==================================================================== 6.5 entitlement enforcement


class TestEntitlementOnToolPath:
    def test_non_entitled_customer_raises_on_the_tool_path(
        self, services: Services, phase5_engine: Engine
    ) -> None:
        ids = all_customer_ids(phase5_engine)
        target = ids[-1]
        # A book that does NOT contain the target: the tool must refuse, exactly as REST does.
        principal = principal_for(Role.RM, BookScope(customer_ids=frozenset(ids[:1])))
        registry = build_tool_registry()
        context = _context(services, principal)

        with pytest.raises(EntitlementError):
            registry.execute("profile", context, {"customer_id": target})

    def test_marketing_masking_matches_rest_exactly(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """The tool path masks a financial profile identically to the REST boundary (6.5, 11.6)."""
        customer_id = _first_customer(phase5_engine)
        principal = principal_for(Role.MARKETING)
        context = _context(services, principal)

        tool_result = _run(registry, "financial_profile", context, {"customer_id": customer_id})

        # Build the equivalent REST payload from the same service and mask it the REST way.
        profile = services.financial.get_financial_profile(customer_id)
        assert profile is not None
        rest_data, rest_masked = _mask_like_rest(
            _FinancialProfileResponse(financial_profile=profile), principal
        )

        assert tool_result.data == rest_data
        assert set(tool_result.masked_fields) == set(rest_masked)

    def test_no_masked_balance_leaks_into_a_fact_for_marketing(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.MARKETING))

        result = _run(registry, "financial_profile", context, {"customer_id": customer_id})

        # Marketing cannot see balances: net_worth_cents must not appear as a raw integer fact.
        for fact in result.facts.facts:
            if fact.field == "net_worth_cents":
                assert (
                    not isinstance(fact.value, int) or fact.value == 0
                ), "a masked balance must not reach a fact as a raw integer"

    def test_graph_neighborhood_redacts_non_entitled_nodes_on_the_tool_path(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        # A customer with at least one relationship, restricted to a book of only themselves.
        target = _customer_with_relationship(phase5_engine, services)
        if target is None:
            pytest.skip("no customer with a relationship in this seed")
        principal = principal_for(Role.RM, BookScope(customer_ids=frozenset({target})))
        context = _context(services, principal)

        result = _run(registry, "graph_neighborhood", context, {"customer_id": target})

        nodes = _data_dict(result)["nodes"]
        # The subject is visible; any counterparty outside the book is structure-only.
        restricted = [n for n in nodes if n["props"].get("restricted") is True]
        assert restricted, "a counterparty outside the book must be redacted to a restricted anchor"
        for node in restricted:
            assert node["entity_id"] == ""


# ==================================================================== 6.4 span instrumentation


class TestToolSpans:
    def test_execution_emits_a_gen_ai_execute_tool_span(
        self,
        services: Services,
        registry: ToolRegistry,
        phase5_engine: Engine,
        span_exporter: InMemorySpanExporter,
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        registry.execute("profile", context, {"customer_id": customer_id})

        span = _find_span(span_exporter, "execute_tool profile")
        assert span is not None
        assert span.attributes["gen_ai.operation.name"] == "execute_tool"
        assert span.attributes["gen_ai.tool.name"] == "profile"
        assert span.attributes["c360.tool"] == "profile"
        assert span.attributes["c360.outcome"] == "ok"

    def test_span_carries_no_argument_values(
        self,
        services: Services,
        registry: ToolRegistry,
        phase5_engine: Engine,
        span_exporter: InMemorySpanExporter,
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        registry.execute("profile", context, {"customer_id": customer_id})

        span = _find_span(span_exporter, "execute_tool profile")
        assert span is not None
        # The customer id (the argument) must not appear in any exported attribute value.
        for value in span.attributes.values():
            assert customer_id not in str(value)


# ==================================================================== 6.3 schema export


class TestSchemaExport:
    def test_schemas_are_function_tool_objects(self, registry: ToolRegistry) -> None:
        schemas = tool_schemas(registry)

        assert len(schemas) == len(registry.names())
        names = {schema["function"]["name"] for schema in schemas}
        assert names == set(registry.names())
        for schema in schemas:
            assert schema["type"] == "function"
            function = schema["function"]
            assert function["description"]
            params = function["parameters"]
            assert params["type"] == "object"
            assert "properties" in params

    def test_schema_inlines_defs(self, registry: ToolRegistry) -> None:
        # holdings has an enum arg (AccountType) Pydantic factors into $defs; it must be inlined.
        holdings = next(s for s in tool_schemas(registry) if s["function"]["name"] == "holdings")
        assert "$defs" not in holdings["function"]["parameters"]


# ==================================================================== customer_cohort (book-level)


class TestCustomerCohortTool:
    """The book-level cohort tool: filter/rank the entitled book, scoped in SQL, no figures out."""

    def test_cohort_returns_display_only_members_and_no_facts(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """A cohort answer is a picker list — display fields only, no citable financial facts."""
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "customer_cohort", context, {"limit": 5})

        data = _data_dict(result)
        members = data["members"]
        assert isinstance(members, list)
        assert data["count"] == len(members)
        for member in members:
            assert set(member) == {
                "customer_id",
                "customer_name",
                "customer_segment",
                "customer_value",
                "risk_band",
                "delinquency_status",
            }
        # No raw balances ride the cohort path, so it carries no citable facts.
        assert result.facts.facts == ()
        # It is not about one customer.
        assert result.entity_id == ""

    def test_cohort_is_scoped_to_the_entitled_book(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """A restricted book never surfaces a customer outside it (requirement 3.3, 12.3)."""
        ids = all_customer_ids(phase5_engine)
        outside_id = ids[-1]
        # A book of only the first customer: no query, however broad, may reach the last one.
        principal = principal_for(Role.RM, BookScope(customer_ids=frozenset(ids[:1])))
        context = _context(services, principal)

        result = _run(registry, "customer_cohort", context, {"limit": 100})

        members = _data_dict(result)["members"]
        assert all(member["customer_id"] != outside_id for member in members)
        assert all(member["customer_id"] == ids[0] for member in members)

    def test_cohort_min_risk_band_returns_only_that_band_or_higher(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """``min_risk_band=ELEVATED`` returns the ELEVATED and HIGH customers only."""
        context = _context(services, principal_for(Role.RM))

        result = _run(
            registry,
            "customer_cohort",
            context,
            {"min_risk_band": RiskBand.ELEVATED.value, "limit": 100},
        )

        members = _data_dict(result)["members"]
        assert members, "the seeded book has ELEVATED-band customers"
        for member in members:
            assert member["risk_band"] in {"ELEVATED", "HIGH"}

    def test_cohort_delinquent_only_returns_past_due_customers(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "customer_cohort", context, {"delinquent_only": True, "limit": 100})

        members = _data_dict(result)["members"]
        assert members, "the seeded book has delinquent customers"
        for member in members:
            assert member["delinquency_status"] not in (None, "CURRENT")

    def test_cohort_segment_filter_restricts_to_named_segment(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        context = _context(services, principal_for(Role.RM))

        result = _run(
            registry,
            "customer_cohort",
            context,
            {"segments": [CustomerSegment.AFFLUENT.value], "limit": 100},
        )

        members = _data_dict(result)["members"]
        assert members, "the seeded book has AFFLUENT customers"
        for member in members:
            assert member["customer_segment"] == "AFFLUENT"

    def test_cohort_respects_a_marketing_segment_scope(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """A SEGMENT-scoped principal only ever sees customers in its allowed segments."""
        allowed = frozenset({CustomerSegment.MASS, CustomerSegment.AFFLUENT})
        principal = principal_for(Role.MARKETING, SegmentScope(segments=allowed))
        context = _context(services, principal)

        result = _run(registry, "customer_cohort", context, {"limit": 100})

        members = _data_dict(result)["members"]
        assert members
        for member in members:
            assert member["customer_segment"] in {"MASS", "AFFLUENT"}


# ==================================================================== pitch (talking points)


class TestPitchTool:
    """The pitch tool: compose offers + financials + risk, masked per role, figures cited."""

    def test_pitch_composes_the_three_sections(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "pitch", context, {"customer_id": customer_id})

        data = _data_dict(result)
        assert set(data) == {"offers", "financial", "risk"}
        assert result.entity_id == customer_id

    def test_pitch_authorizes_the_customer(self, services: Services, phase5_engine: Engine) -> None:
        """The pitch runs the same 403/404 gate every read does (requirement 11.6)."""
        ids = all_customer_ids(phase5_engine)
        target = ids[-1]
        principal = principal_for(Role.RM, BookScope(customer_ids=frozenset(ids[:1])))
        registry = build_tool_registry()
        context = _context(services, principal)

        with pytest.raises(EntitlementError):
            registry.execute("pitch", context, {"customer_id": target})

    def test_pitch_money_facts_stay_integer_cents(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.RM))

        result = _run(registry, "pitch", context, {"customer_id": customer_id})

        for fact in result.facts.facts:
            if fact.field.endswith("_cents"):
                assert isinstance(fact.value, int)

    def test_pitch_does_not_leak_a_masked_balance_into_a_fact_for_marketing(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """A balance Marketing cannot see (BALANCES group) must not reach a fact as a raw integer.

        Offer expected value is an OFFERS-group field Marketing *can* see, so it is not asserted on
        here; the invariant is that the pitch masks exactly as the underlying tools do, which the
        parity check below pins down. This test guards the balance specifically.
        """
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.MARKETING))

        result = _run(registry, "pitch", context, {"customer_id": customer_id})

        for fact in result.facts.facts:
            if fact.field == "net_worth_cents":
                assert (
                    not isinstance(fact.value, int) or fact.value == 0
                ), "a masked balance must not reach a fact as a raw integer"

    def test_pitch_masks_financials_exactly_like_the_financial_profile_tool(
        self, services: Services, registry: ToolRegistry, phase5_engine: Engine
    ) -> None:
        """The pitch's financial facts match what the standalone financial_profile tool would cite.

        This pins the parity that lets the masking-specific assertions above stay narrow: the pitch
        does not re-decide masking, it reuses the same serializer path, so a role sees the same
        financial figures cited from a pitch as from the dedicated tool.
        """
        customer_id = _first_customer(phase5_engine)
        context = _context(services, principal_for(Role.MARKETING))

        pitch = _run(registry, "pitch", context, {"customer_id": customer_id})
        financial = _run(registry, "financial_profile", context, {"customer_id": customer_id})

        pitch_fin = {
            (f.field, f.value) for f in pitch.facts.facts if f.entity_type == "financial_profile"
        }
        financial_fin = {
            (f.field, f.value)
            for f in financial.facts.facts
            if f.entity_type == "financial_profile"
        }
        assert pitch_fin, "the pitch cites financial figures"
        # Every financial figure the pitch cites is one the dedicated tool cites identically —
        # same value, same masking — so the pitch introduces no divergent view.
        assert pitch_fin <= financial_fin


# ==================================================================== helpers


def _mask_like_rest(model: Any, principal: Principal) -> tuple[Any, list[str]]:
    """The masking the REST boundary applies, without the envelope, for a parity assertion."""
    return mask_model(model, principal.field_policy)


def _customer_with_relationship(phase5_engine: Engine, services: Services) -> str | None:
    row = query_one(
        phase5_engine,
        "SELECT from_customer_id FROM customer_relationship LIMIT 1",
    )
    return None if row is None else str(row[0])


def _find_span(exporter: InMemorySpanExporter, name: str) -> Any:
    for span in exporter.get_finished_spans():
        if span.name == name:
            return span
    return None
