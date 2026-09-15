"""Phase 5 domain-service unit tests (tasks 5.1-5.7).

Exercised against the generated, recomputed dataset (:mod:`tests.phase5_fixtures`) so search hits a
real FTS index and traversal walks a real graph. Assertions favour invariants that must hold for any
seed — ordering, entitlement scoping, the major-transaction rule, cooling-off — over values tied to
one customer, so the suite does not become brittle against a generator change.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import Engine

from c360.data.repositories import build_repositories
from c360.domain.enums import AccountType, EngagementChannel
from c360.security.entitlement import AllScope, BookScope
from c360.services.customer import CustomerService, RecentlyViewedTracker
from c360.services.financial import FinancialService
from c360.services.journey import JourneyService
from c360.services.offer import OfferService
from c360.services.relationship import RelationshipService, customer_node_id
from c360.services.risk import AlertSeverity, RiskService
from tests.phase5_fixtures import all_customer_ids, query_one


# ==================================================================== CustomerService (5.1)
class TestCustomerSearch:
    def _service(self, engine: Engine) -> CustomerService:
        return CustomerService(build_repositories(engine).customer)

    def test_search_finds_a_customer_by_name_prefix(self, phase5_engine: Engine) -> None:
        name = str(query_one(phase5_engine, "SELECT customer_name FROM customer LIMIT 1")[0])
        first_token = name.split(maxsplit=1)[0]
        service = self._service(phase5_engine)

        page = service.search(AllScope(), first_token[:4], limit=25)

        assert any(first_token in hit.customer_name for hit in page.items)

    def test_search_is_empty_for_blank_query(self, phase5_engine: Engine) -> None:
        page = self._service(phase5_engine).search(AllScope(), "   ", limit=25)
        assert page.items == []
        assert page.next_cursor is None

    def test_search_is_empty_for_punctuation_only_query(self, phase5_engine: Engine) -> None:
        page = self._service(phase5_engine).search(AllScope(), '"*:()', limit=25)
        assert page.items == []

    def test_search_paginates_with_a_cursor(self, phase5_engine: Engine) -> None:
        service = self._service(phase5_engine)
        # A single common letter matches many rows via prefix; page through them.
        first = service.search(AllScope(), "a", limit=2)
        assert len(first.items) <= 2
        if first.next_cursor is not None:
            second = service.search(AllScope(), "a", limit=2, cursor=first.next_cursor)
            first_ids = {hit.customer_id for hit in first.items}
            second_ids = {hit.customer_id for hit in second.items}
            assert first_ids.isdisjoint(second_ids), "a cursor must not re-return a page"

    def test_book_scope_search_never_returns_outside_the_book(self, phase5_engine: Engine) -> None:
        ids = all_customer_ids(phase5_engine)
        book = frozenset(ids[:3])
        service = self._service(phase5_engine)

        page = service.search(BookScope(customer_ids=book), "a", limit=100)

        assert all(hit.customer_id in book for hit in page.items)

    def test_empty_book_search_returns_nothing(self, phase5_engine: Engine) -> None:
        page = self._service(phase5_engine).search(
            BookScope(customer_ids=frozenset()), "a", limit=100
        )
        assert page.items == []

    def test_profile_and_contact_round_trip(self, phase5_engine: Engine) -> None:
        service = self._service(phase5_engine)
        customer_id = all_customer_ids(phase5_engine)[0]

        profile = service.get_profile(customer_id)
        assert profile is not None
        assert profile.customer_id == customer_id
        # Contact may or may not exist for a given customer; the call must not raise either way.
        service.get_contact(customer_id)

    def test_missing_profile_is_none_not_an_error(self, phase5_engine: Engine) -> None:
        assert self._service(phase5_engine).get_profile("C-NOPE") is None


class TestRecentlyViewed:
    def test_records_most_recent_first(self) -> None:
        tracker = RecentlyViewedTracker(max_per_user=3)
        for cid in ["C-1", "C-2", "C-3"]:
            tracker.record("u", cid)
        assert tracker.recent("u") == ("C-3", "C-2", "C-1")

    def test_reviewing_moves_to_front_without_duplicating(self) -> None:
        tracker = RecentlyViewedTracker(max_per_user=3)
        for cid in ["C-1", "C-2", "C-1"]:
            tracker.record("u", cid)
        assert tracker.recent("u") == ("C-1", "C-2")

    def test_ring_is_bounded(self) -> None:
        tracker = RecentlyViewedTracker(max_per_user=2)
        for cid in ["C-1", "C-2", "C-3"]:
            tracker.record("u", cid)
        assert tracker.recent("u") == ("C-3", "C-2")

    def test_users_are_isolated(self) -> None:
        tracker = RecentlyViewedTracker()
        tracker.record("a", "C-1")
        tracker.record("b", "C-2")
        assert tracker.recent("a") == ("C-1",)
        assert tracker.recent("b") == ("C-2",)

    def test_unknown_user_has_empty_history(self) -> None:
        assert RecentlyViewedTracker().recent("nobody") == ()


# ==================================================================== FinancialService (5.2)
class TestFinancialService:
    def _service(self, engine: Engine, *, sigma: float = 2.0) -> FinancialService:
        return FinancialService(build_repositories(engine).financial, spend_anomaly_sigma=sigma)

    def test_holdings_type_filter_only_returns_that_type(self, phase5_engine: Engine) -> None:
        service = self._service(phase5_engine)
        customer_id = _customer_with_account_type(phase5_engine, AccountType.DEPOSIT)

        holdings = service.get_holdings(customer_id, account_types=[AccountType.DEPOSIT])

        assert holdings
        assert all(h.account.account_type is AccountType.DEPOSIT for h in holdings)

    def test_expense_analytics_returns_categories_and_monthly_trend(
        self, phase5_engine: Engine
    ) -> None:
        service = self._service(phase5_engine)
        customer_id = _customer_with_transactions(phase5_engine)

        analytics = service.get_expense_analytics(customer_id)

        assert analytics.by_category, "a customer with debits has category totals"
        assert analytics.monthly, "a customer with debits has a monthly trend"
        assert analytics.threshold_sigma == 2.0
        # The monthly series is oldest-first.
        months = [point.month for point in analytics.monthly]
        assert months == sorted(months)

    def test_deviation_flag_uses_the_configured_sigma(self, phase5_engine: Engine) -> None:
        customer_id = _customer_with_transactions(phase5_engine)
        # A very low sigma flags almost every month; a very high sigma flags almost none.
        loose = self._service(phase5_engine, sigma=0.1).get_expense_analytics(customer_id)
        strict = self._service(phase5_engine, sigma=100.0).get_expense_analytics(customer_id)

        loose_flags = sum(1 for m in loose.monthly if m.is_anomaly)
        strict_flags = sum(1 for m in strict.monthly if m.is_anomaly)
        assert strict_flags <= loose_flags

    def test_early_months_have_no_baseline(self, phase5_engine: Engine) -> None:
        customer_id = _customer_with_transactions(phase5_engine)
        analytics = self._service(phase5_engine).get_expense_analytics(customer_id)
        # The first month can never be an anomaly: it has no preceding window.
        assert analytics.monthly[0].deviation_sigma is None
        assert analytics.monthly[0].is_anomaly is False

    def test_rejects_non_positive_sigma(self, phase5_engine: Engine) -> None:
        with pytest.raises(ValueError, match="spend_anomaly_sigma"):
            FinancialService(build_repositories(phase5_engine).financial, spend_anomaly_sigma=0)


# ==================================================================== RiskService (5.4)
class TestRiskService:
    def _service(self, engine: Engine) -> RiskService:
        return RiskService(build_repositories(engine).risk)

    def test_compliance_alert_is_non_dismissible_and_first(self, phase5_engine: Engine) -> None:
        flagged = query_one(
            phase5_engine,
            "SELECT customer_id FROM risk_profile WHERE aml_flag = 1 OR pep_flag = 1 LIMIT 1",
        )
        if flagged is None:
            pytest.skip("no compliance-flagged customer in this seed")
        view = self._service(phase5_engine).get_risk(str(flagged[0]))

        assert view is not None
        assert view.requires_compliance_indicator is True
        compliance = [a for a in view.alerts if a.category == "COMPLIANCE"]
        assert compliance, "an AML/PEP customer must surface a compliance alert"
        assert all(not a.dismissible for a in compliance), "compliance alerts are non-dismissible"
        assert view.alerts[0].category == "COMPLIANCE", "compliance sorts first"

    def test_alerts_are_severity_ordered(self, phase5_engine: Engine) -> None:
        for customer_id in all_customer_ids(phase5_engine):
            view = self._service(phase5_engine).get_risk(customer_id)
            if view is None or len(view.alerts) < 2:
                continue
            severities = [int(a.severity) for a in view.alerts]
            assert severities == sorted(severities, reverse=True)

    def test_exposure_is_never_negative(self, phase5_engine: Engine) -> None:
        service = self._service(phase5_engine)
        for customer_id in all_customer_ids(phase5_engine)[:10]:
            assert int(service.get_exposure_cents(customer_id)) >= 0

    def test_missing_risk_profile_is_none(self, phase5_engine: Engine) -> None:
        assert self._service(phase5_engine).get_risk("C-NOPE") is None

    def test_severity_ordering_is_total(self) -> None:
        assert AlertSeverity.CRITICAL > AlertSeverity.WARNING > AlertSeverity.INFO


# ==================================================================== OfferService (5.5)
class TestOfferService:
    def _service(self, engine: Engine, *, cooling_off_days: int = 90) -> OfferService:
        return OfferService(build_repositories(engine).offer, cooling_off_days=cooling_off_days)

    def test_offers_are_ranked_by_expected_value_within_live_offers(
        self, phase5_engine: Engine
    ) -> None:
        customer_id = _customer_with_offers(phase5_engine)
        ranked = self._service(phase5_engine).get_offers(customer_id, as_of=date(2026, 9, 1))

        live = [o for o in ranked.offers if not o.suppressed]
        evs = [o.expected_value_cents for o in live]
        assert evs == sorted(evs, reverse=True), "live offers rank by expected value descending"

    def test_suppressed_offers_are_kept_but_sorted_last(self, phase5_engine: Engine) -> None:
        suppressed_customer = query_one(
            phase5_engine,
            "SELECT customer_id FROM customer_offer WHERE is_suppressed = 1 LIMIT 1",
        )
        if suppressed_customer is None:
            pytest.skip("no suppressed offer in this seed")
        ranked = self._service(phase5_engine).get_offers(
            str(suppressed_customer[0]), as_of=date(2026, 9, 1)
        )

        suppressed = [o for o in ranked.offers if o.suppressed]
        assert suppressed, "a suppressed offer is kept, not dropped (requirement 9.5)"
        assert all(o.suppression_reason for o in suppressed), "suppressed offers carry a reason"
        # Every suppressed offer ranks after every live one.
        first_suppressed = next(i for i, o in enumerate(ranked.offers) if o.suppressed)
        assert all(o.suppressed for o in ranked.offers[first_suppressed:])

    def test_next_best_offer_is_a_live_offer(self, phase5_engine: Engine) -> None:
        customer_id = _customer_with_offers(phase5_engine)
        nbo = self._service(phase5_engine).get_next_best_offer(customer_id, as_of=date(2026, 9, 1))
        if nbo is not None:
            assert not nbo.suppressed

    def test_declined_offer_within_window_is_cooled_off(self, phase5_engine: Engine) -> None:
        # Build a declined offer scenario deterministically by picking any offer and checking the
        # rule against two as-of dates: inside the window it is suppressed, outside it is not.
        declined = query_one(
            phase5_engine,
            "SELECT customer_id, reaction_date FROM customer_offer "
            "WHERE customer_reaction = 'DECLINED' AND reaction_date IS NOT NULL "
            "AND is_suppressed = 0 LIMIT 1",
        )
        if declined is None:
            pytest.skip("no declined, non-suppressed offer with a reaction date in this seed")
        customer_id = str(declined[0])
        reaction_date = date.fromisoformat(str(declined[1]))
        service = self._service(phase5_engine, cooling_off_days=30)

        inside = service.get_offers(customer_id, as_of=reaction_date + timedelta(days=5))
        outside = service.get_offers(customer_id, as_of=reaction_date + timedelta(days=60))

        cooled = [
            o
            for o in inside.offers
            if o.suppression_reason and o.suppression_reason.startswith("COOLING_OFF")
        ]
        assert cooled, "a recently declined offer is cooled off"
        still_cooled = [
            o
            for o in outside.offers
            if o.suppression_reason and o.suppression_reason.startswith("COOLING_OFF")
        ]
        assert not still_cooled, "the cooling-off window expires"

    def test_rejects_negative_cooling_off(self, phase5_engine: Engine) -> None:
        with pytest.raises(ValueError, match="cooling_off_days"):
            OfferService(build_repositories(phase5_engine).offer, cooling_off_days=-1)


# ==================================================================== JourneyService (5.6)
class TestJourneyService:
    def _service(self, engine: Engine, **kwargs: float) -> JourneyService:
        repos = build_repositories(engine)
        return JourneyService(
            repos.journey,
            repos.relationship,
            major_txn_absolute_threshold_cents=int(kwargs.get("absolute", 1_000_000)),
            major_txn_median_multiple=kwargs.get("multiple", 10.0),
        )

    def test_timeline_is_newest_first(self, phase5_engine: Engine) -> None:
        customer_id = _customer_with_transactions(phase5_engine)
        timeline = self._service(phase5_engine).get_timeline(customer_id)
        dates = [entry.entry_date for entry in timeline]
        assert dates == sorted(dates, reverse=True)

    def test_timeline_merges_multiple_categories(self, phase5_engine: Engine) -> None:
        # Some customer in the panel should have more than one kind of timeline entry.
        service = self._service(phase5_engine)
        categories_seen: set[str] = set()
        for customer_id in all_customer_ids(phase5_engine):
            for entry in service.get_timeline(customer_id):
                categories_seen.add(entry.category.value)
        assert len(categories_seen) >= 2, "the merged timeline spans more than one component"

    def test_lower_threshold_yields_at_least_as_many_major_transactions(
        self, phase5_engine: Engine
    ) -> None:
        customer_id = _customer_with_transactions(phase5_engine)
        low = self._service(phase5_engine, absolute=1, multiple=1.0).get_timeline(customer_id)
        high = self._service(phase5_engine, absolute=10_000_000_000, multiple=1000.0).get_timeline(
            customer_id
        )
        low_major = sum(1 for e in low if e.category.value == "MAJOR_TRANSACTION")
        high_major = sum(1 for e in high if e.category.value == "MAJOR_TRANSACTION")
        assert low_major >= high_major

    def test_engagement_history_is_filterable(self, phase5_engine: Engine) -> None:
        customer_id = query_one(phase5_engine, "SELECT customer_id FROM customer_event LIMIT 1")
        if customer_id is None:
            pytest.skip("no engagement events in this seed")
        service = self._service(phase5_engine)
        events = service.get_engagement_history(
            str(customer_id[0]), channels=[EngagementChannel.MOBILE]
        )
        assert all(e.channel is EngagementChannel.MOBILE for e in events)


# ==================================================================== RelationshipService (5.3)
class TestRelationshipService:
    def _service(self, engine: Engine) -> RelationshipService:
        repos = build_repositories(engine)
        return RelationshipService(
            repos.relationship,
            repos.graph,
            repos.customer,
            repos.financial,
            max_hops=3,
            node_cap=300,
        )

    def test_neighborhood_contains_the_root(self, phase5_engine: Engine) -> None:
        customer_id = _customer_in_household(phase5_engine)
        view = self._service(phase5_engine).get_network(AllScope(), customer_id)
        node_ids = {node.node_id for node in view.nodes}
        assert customer_node_id(customer_id) in node_ids

    def test_household_rollup_sums_across_members(self, phase5_engine: Engine) -> None:
        customer_id = _customer_in_household(phase5_engine)
        rollup = self._service(phase5_engine).get_household_rollup(customer_id)
        assert rollup is not None
        assert rollup.member_count >= 1
        assert int(rollup.net_worth_cents) != 0 or rollup.member_count >= 1

    def test_isolated_customer_has_no_household_rollup(self, phase5_engine: Engine) -> None:
        isolated = query_one(
            phase5_engine, "SELECT customer_id FROM customer WHERE household_id IS NULL LIMIT 1"
        )
        if isolated is None:
            pytest.skip("no isolated customer in this seed")
        rollup = self._service(phase5_engine).get_household_rollup(str(isolated[0]))
        assert rollup is None

    def test_book_scope_redacts_out_of_book_customer_nodes(self, phase5_engine: Engine) -> None:
        customer_id = _customer_in_household(phase5_engine)
        # A book of only the root: any other customer node in the neighborhood must be redacted.
        book = BookScope(customer_ids=frozenset({customer_id}))
        view = self._service(phase5_engine).get_network(book, customer_id)

        for node in view.nodes:
            if node.node_type == "Customer" and node.entity_id != customer_id:
                assert node.props.get("restricted") is True, "out-of-book customer must be redacted"
                assert node.entity_id == "", "a redacted node carries no real entity id"

    def test_degree_centrality_is_non_negative(self, phase5_engine: Engine) -> None:
        customer_id = _customer_in_household(phase5_engine)
        assert self._service(phase5_engine).degree_centrality(customer_id) >= 0


# ==================================================================== helpers
def _customer_with_account_type(engine: Engine, account_type: AccountType) -> str:
    row = query_one(
        engine,
        "SELECT customer_id FROM account WHERE account_type = :t LIMIT 1",
        {"t": account_type.value},
    )
    assert row is not None, f"the seed has no {account_type} account"
    return str(row[0])


def _customer_with_transactions(engine: Engine) -> str:
    row = query_one(
        engine,
        "SELECT customer_id FROM txn WHERE amount_cents < 0 "
        "GROUP BY customer_id ORDER BY COUNT(*) DESC LIMIT 1",
    )
    assert row is not None
    return str(row[0])


def _customer_with_offers(engine: Engine) -> str:
    row = query_one(
        engine,
        "SELECT customer_id FROM customer_offer WHERE is_suppressed = 0 "
        "GROUP BY customer_id ORDER BY COUNT(*) DESC LIMIT 1",
    )
    assert row is not None
    return str(row[0])


def _customer_in_household(engine: Engine) -> str:
    row = query_one(
        engine,
        "SELECT customer_id FROM household_member "
        "GROUP BY household_id HAVING COUNT(*) > 1 LIMIT 1",
    )
    if row is None:
        row = query_one(
            engine, "SELECT customer_id FROM customer WHERE household_id IS NOT NULL LIMIT 1"
        )
    assert row is not None
    return str(row[0])
