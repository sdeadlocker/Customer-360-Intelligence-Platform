"""Task 4.3: entitlement scoping applied inside repository queries, and the 403/404 distinction."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from c360.data.engine import AccessMode, create_sqlite_engine
from c360.data.migrations import upgrade_database
from c360.data.repositories.customer import SqliteCustomerRepository
from c360.domain.enums import CustomerSegment
from c360.security.authorization import authorize_customer, is_customer_visible
from c360.security.entitlement import AllScope, BookScope, SegmentScope
from c360.security.errors import EntitlementError
from c360.security.model import Principal, Role
from c360.security.policy import policy_for_role
from tests import seed_data
from tests.seed_data import PRIMARY_ID, SECONDARY_ID


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("entitlement") / "customer.db"
    upgrade_database(path)
    writer = create_sqlite_engine(path, mode=AccessMode.READ_WRITE, pool_size=1)
    try:
        with writer.begin() as connection:
            seed_data.seed(connection)
    finally:
        writer.dispose()
    return path


@pytest.fixture
def repo(seeded_db: Path) -> Iterator[SqliteCustomerRepository]:
    engine = create_sqlite_engine(seeded_db, mode=AccessMode.READ_ONLY, pool_size=2)
    try:
        yield SqliteCustomerRepository(engine)
    finally:
        engine.dispose()


def _principal(role: Role, scope: object) -> Principal:
    return Principal(
        user_id=f"{role}.user",
        role=role,
        entitlement=scope,  # type: ignore[arg-type]
        field_policy=policy_for_role(role),
        knowledge_levels=frozenset(),
    )


class TestVisibility:
    def test_all_scope_sees_every_customer(self, repo: SqliteCustomerRepository) -> None:
        scope = AllScope()
        assert repo.visible_to(scope, PRIMARY_ID)
        assert repo.visible_to(scope, SECONDARY_ID)

    def test_all_scope_still_reports_a_nonexistent_customer_as_invisible(
        self, repo: SqliteCustomerRepository
    ) -> None:
        assert not repo.visible_to(AllScope(), "C-9999")

    def test_book_scope_sees_only_its_book(self, repo: SqliteCustomerRepository) -> None:
        scope = BookScope(customer_ids=frozenset({PRIMARY_ID}))
        assert repo.visible_to(scope, PRIMARY_ID)
        assert not repo.visible_to(scope, SECONDARY_ID)

    def test_empty_book_sees_nothing(self, repo: SqliteCustomerRepository) -> None:
        assert not repo.visible_to(BookScope(customer_ids=frozenset()), PRIMARY_ID)

    def test_segment_scope_matches_on_segment(self, repo: SqliteCustomerRepository) -> None:
        affluent = SegmentScope(segments=frozenset({CustomerSegment.AFFLUENT}))
        # C-0001 is AFFLUENT, C-0002 is MASS.
        assert repo.visible_to(affluent, PRIMARY_ID)
        assert not repo.visible_to(affluent, SECONDARY_ID)

        mass = SegmentScope(segments=frozenset({CustomerSegment.MASS}))
        assert repo.visible_to(mass, SECONDARY_ID)
        assert not repo.visible_to(mass, PRIMARY_ID)


class TestScopedListing:
    def test_all_scope_lists_everyone(self, repo: SqliteCustomerRepository) -> None:
        ids = repo.list_ids_scoped(AllScope(), limit=100)
        assert set(ids) == {PRIMARY_ID, SECONDARY_ID}

    def test_book_scope_lists_only_its_book(self, repo: SqliteCustomerRepository) -> None:
        ids = repo.list_ids_scoped(BookScope(customer_ids=frozenset({SECONDARY_ID})), limit=100)
        assert list(ids) == [SECONDARY_ID]

    def test_a_book_of_one_does_not_reveal_the_other_via_count(
        self, repo: SqliteCustomerRepository
    ) -> None:
        """Requirement 3.3: a restricted book must not reveal the existence of non-entitled rows."""
        ids = repo.list_ids_scoped(BookScope(customer_ids=frozenset({PRIMARY_ID})), limit=100)
        assert len(ids) == 1

    def test_scope_is_applied_before_limit(self, repo: SqliteCustomerRepository) -> None:
        """A limit of 1 on a book that excludes the first-ordered customer still returns the book.

        C-0001 orders before C-0002. A post-filter would fetch C-0001, then drop it, and return
        nothing. An in-query predicate skips C-0001 in SQL and returns C-0002.
        """
        ids = repo.list_ids_scoped(BookScope(customer_ids=frozenset({SECONDARY_ID})), limit=1)
        assert list(ids) == [SECONDARY_ID]

    def test_segment_scope_listing(self, repo: SqliteCustomerRepository) -> None:
        ids = repo.list_ids_scoped(
            SegmentScope(segments=frozenset({CustomerSegment.MASS})), limit=100
        )
        assert list(ids) == [SECONDARY_ID]


class TestAuthorize:
    def test_entitled_customer_is_allowed(self, repo: SqliteCustomerRepository) -> None:
        principal = _principal(Role.RM, BookScope(customer_ids=frozenset({PRIMARY_ID})))
        authorize_customer(principal, PRIMARY_ID, repo)  # does not raise

    def test_non_entitled_customer_raises(self, repo: SqliteCustomerRepository) -> None:
        principal = _principal(Role.RM, BookScope(customer_ids=frozenset({PRIMARY_ID})))
        with pytest.raises(EntitlementError):
            authorize_customer(principal, SECONDARY_ID, repo)

    def test_nonexistent_customer_raises_the_same_way(self, repo: SqliteCustomerRepository) -> None:
        """A non-entitled probe cannot tell 'does not exist' from 'not entitled' (req 15.5)."""
        principal = _principal(Role.RM, BookScope(customer_ids=frozenset({PRIMARY_ID})))
        with pytest.raises(EntitlementError):
            authorize_customer(principal, "C-9999", repo)

    def test_is_visible_is_the_nonraising_form(self, repo: SqliteCustomerRepository) -> None:
        principal = _principal(Role.RISK, AllScope())
        assert is_customer_visible(principal, PRIMARY_ID, repo)
        blind = _principal(Role.RM, BookScope(customer_ids=frozenset()))
        assert not is_customer_visible(blind, PRIMARY_ID, repo)
