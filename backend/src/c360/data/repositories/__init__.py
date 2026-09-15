"""SQLite implementations of the repository ports in :mod:`c360.domain.ports` (task 1.6).

One adapter per aggregate, each a thin, read-only translation between SQL rows and domain models.
:mod:`c360.data.repositories.base` holds the two things they all need — span instrumentation keyed
on
a statement ID, and row-to-model validation.

:func:`build_repositories` is the composition point. Nothing above the data layer constructs an
adapter directly, so the engine is created once per process and shared, rather than a service
opening
its own connection pool and quietly doubling the file handles design §12.3 budgets for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from c360.data.repositories.base import (
    DB_SYSTEM,
    QueryMetrics,
    RepositoryError,
    SqliteRepository,
    Statement,
    expand_in_clause,
    optional_model,
    to_model,
    to_models,
)
from c360.data.repositories.customer import SqliteCustomerRepository
from c360.data.repositories.financial import SqliteFinancialRepository
from c360.data.repositories.graph import SqliteGraphRepository
from c360.data.repositories.journey import SqliteJourneyRepository
from c360.data.repositories.offer import SqliteOfferRepository
from c360.data.repositories.relationship import SqliteRelationshipRepository
from c360.data.repositories.risk import SqliteRiskRepository

if TYPE_CHECKING:
    from sqlalchemy import Engine


@dataclass(frozen=True, slots=True)
class Repositories:
    """The six adapters over one engine.

    Typed as the concrete classes rather than as the protocols so that ``mypy`` verifies conformance
    at the point of construction: if an adapter drifts from its port, the assignment in
    :func:`build_repositories` fails to type-check. Consumers should still annotate against the
    protocols in :mod:`c360.domain.ports`.
    """

    customer: SqliteCustomerRepository
    financial: SqliteFinancialRepository
    relationship: SqliteRelationshipRepository
    risk: SqliteRiskRepository
    offer: SqliteOfferRepository
    journey: SqliteJourneyRepository
    graph: SqliteGraphRepository


def build_repositories(engine: Engine, *, metrics: QueryMetrics | None = None) -> Repositories:
    """Construct every adapter over ``engine``."""
    return Repositories(
        customer=SqliteCustomerRepository(engine, metrics=metrics),
        financial=SqliteFinancialRepository(engine, metrics=metrics),
        relationship=SqliteRelationshipRepository(engine, metrics=metrics),
        risk=SqliteRiskRepository(engine, metrics=metrics),
        offer=SqliteOfferRepository(engine, metrics=metrics),
        journey=SqliteJourneyRepository(engine, metrics=metrics),
        graph=SqliteGraphRepository(engine, metrics=metrics),
    )


__all__ = [
    "DB_SYSTEM",
    "QueryMetrics",
    "Repositories",
    "RepositoryError",
    "SqliteCustomerRepository",
    "SqliteFinancialRepository",
    "SqliteGraphRepository",
    "SqliteJourneyRepository",
    "SqliteOfferRepository",
    "SqliteRelationshipRepository",
    "SqliteRepository",
    "SqliteRiskRepository",
    "Statement",
    "build_repositories",
    "expand_in_clause",
    "optional_model",
    "to_model",
    "to_models",
]
