"""Shared machinery for the SQLite repository adapters (task 1.6).

Two concerns live here so no individual repository has to get them right: instrumenting a read as a
span, and mapping a row onto a Pydantic model.

Spans carry a statement ID, never a statement
---------------------------------------------

Requirement 18.1 wants repository calls visible in traces. The obvious way to do that is to put the
SQL on the span as ``db.statement`` — and the allowlist in :mod:`c360.core.telemetry.allowlist`
explicitly refuses it, "*because bound values would ride along*". Even parameterized SQL is a hazard
here: an ``IN`` clause built from a list of account types is assembled with generated placeholders,
and an interpolated ``LIMIT`` is one careless edit away from being an interpolated ``WHERE``.

So each statement is declared once, at module level, with a stable ID, and the span carries the ID.
A trace answers "which read was slow" precisely, and the mapping from ID to SQL is in source where
it
can be reviewed. The ID also keeps span-name cardinality bounded, which design §13.4 requires of
anything that becomes a metric dimension.

Pool wait is measured separately from query time
------------------------------------------------

``c360.db.pool_wait_ms`` is recorded because the pool is bounded with no overflow (design §12.3), so
under concurrency the interesting latency is time spent waiting for a connection rather than time
spent executing. Those two look identical in a single duration, and they have opposite fixes: one is
a pool-size problem, the other is an index problem.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import Engine, Row, text
from sqlalchemy.exc import SQLAlchemyError

from c360.core.logging import get_logger
from c360.core.telemetry import SpanAttr, get_metrics, get_tracer
from c360.domain.models import DomainModel

_logger = get_logger(__name__)


#: Instrumentation scope for every span this module creates.
TRACER_NAME: Final = "c360.data.repositories"

#: Value of ``db.system.name`` for every span this module creates.
DB_SYSTEM: Final = "sqlite"


class RepositoryError(RuntimeError):
    """A read failed at the storage layer.

    Wraps the driver exception so that services and the aggregator (design §5.7, which isolates a
    per-domain failure into ``meta.errors[]``) can catch one type without importing SQLAlchemy. The
    original is chained, and the message names the statement ID rather than reproducing the SQL —
    an exception message ends up in logs, and design §13.4 keeps statements out of telemetry.
    """


@dataclass(frozen=True, slots=True)
class Statement:
    """One declared SQL read.

    Args:
        id: Stable identifier, ``aggregate.purpose`` (e.g. ``customer.get``). Appears on the span as
            ``c360.db.statement_id``; changing it breaks trace continuity, so treat it as an
            interface.
        sql: The SQL. Parameterized only — see :meth:`SqliteRepository.fetch_all` for how variable
            length ``IN`` lists are handled without string building.
        collection: Primary table, for ``db.collection.name``.
        operation: SQL verb, for ``db.operation.name``. Always a read in this layer.
    """

    id: str
    sql: str
    collection: str
    operation: str = "SELECT"

    @property
    def span_name(self) -> str:
        """``SELECT customer`` — the low-cardinality form OTel's database conventions ask for."""
        return f"{self.operation} {self.collection}"


def expand_in_clause(name: str, values: Sequence[object]) -> tuple[str, dict[str, object]]:
    """Build a parameterized ``IN`` list and its bindings.

    SQLite has no array type and no way to bind a list to a single placeholder, so a variable-length
    ``IN`` needs one placeholder per value. Generating the *placeholders* is safe; generating the
    values into the SQL is not, and the two look similar enough in a diff that this is the only
    place
    allowed to do the former.

    Returns a fragment such as ``:kind_0, :kind_1`` together with ``{"kind_0": ..., "kind_1":
    ...}``.
    """
    if not values:
        raise ValueError(f"expand_in_clause needs at least one value for {name!r}")
    placeholders = ", ".join(f":{name}_{index}" for index in range(len(values)))
    bindings: dict[str, object] = {f"{name}_{index}": value for index, value in enumerate(values)}
    return placeholders, bindings


@dataclass(slots=True)
class QueryMetrics:
    """Counters a test can assert on without a span exporter."""

    statements: list[str] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)


class SqliteRepository:
    """Base for the SQLite adapters.

    Holds the engine and nothing else. Repositories are cheap to construct and hold no per-request
    state, so one instance per engine can be shared across the thread pool the aggregator fans out
    over — the pool, not the repository, is what bounds concurrency.
    """

    __slots__ = ("_engine", "_metrics")

    def __init__(self, engine: Engine, *, metrics: QueryMetrics | None = None) -> None:
        self._engine = engine
        # Optional so production pays nothing for it; tests pass one in to assert which statements
        # a service actually issued, which is how the "no N+1" assertions stay honest.
        self._metrics = metrics

    @property
    def engine(self) -> Engine:
        return self._engine

    # ---------------------------------------------------------------- execution
    def fetch_all(
        self,
        statement: Statement,
        params: Mapping[str, Any] | None = None,
    ) -> list[Row[Any]]:
        """Execute ``statement`` and return every row, inside an instrumented span.

        The tracer is resolved per call rather than cached at module import. Before
        :func:`~c360.core.telemetry.setup_telemetry` runs, ``trace.get_tracer`` hands back a proxy
        that resolves to the global provider on first use *and caches it* — so a module-level tracer
        captured at import time binds permanently to whatever provider existed then. That is a real
        production path, not just a test artifact: the CLI entry points (seed, recompute, eval)
        import
        :mod:`c360.data` before configuring telemetry, and every repository span for the life of the
        process would go to a no-op tracer. A provider lookup is a dictionary access; a SQL round
        trip
        is not, so the cost is not measurable here.
        """
        tracer = get_tracer(TRACER_NAME)
        with tracer.start_as_current_span(statement.span_name) as span:
            span.set_attribute("db.system.name", DB_SYSTEM)
            span.set_attribute("db.operation.name", statement.operation)
            span.set_attribute("db.collection.name", statement.collection)
            span.set_attribute(SpanAttr.DB_STATEMENT_ID, statement.id)

            waited_from = time.perf_counter()
            try:
                with self._engine.connect() as connection:
                    pool_wait_ms = (time.perf_counter() - waited_from) * 1000
                    span.set_attribute(SpanAttr.DB_POOL_WAIT_MS, round(pool_wait_ms, 3))
                    query_from = time.perf_counter()
                    rows = list(connection.execute(text(statement.sql), dict(params or {})).all())
                    query_ms = (time.perf_counter() - query_from) * 1000
            except SQLAlchemyError as exc:
                span.set_attribute(SpanAttr.OUTCOME, "error")
                # `exc` is not interpolated into the message: a SQLAlchemy error string includes the
                # statement and, for an IntegrityError, the bound values.
                _logger.warning(
                    "repository read failed",
                    extra={"statement_id": statement.id, "error_type": type(exc).__name__},
                )
                raise RepositoryError(f"read {statement.id} failed: {type(exc).__name__}") from exc

            span.set_attribute(SpanAttr.DB_ROW_COUNT, len(rows))
            span.set_attribute(SpanAttr.OUTCOME, "ok")

        # Pool checkout wait and query duration by statement id (task 10.2, design §13.3 Database
        # row). The statement id is a bounded, non-identifying label; bound values never appear.
        db_metrics = get_metrics()
        if db_metrics is not None:
            db_metrics.record_pool_wait(wait_ms=pool_wait_ms)
            db_metrics.record_query(statement_id=statement.id, duration_ms=query_ms)

        if self._metrics is not None:
            self._metrics.statements.append(statement.id)
            self._metrics.rows.append(len(rows))
        return rows

    def fetch_one(
        self,
        statement: Statement,
        params: Mapping[str, Any] | None = None,
    ) -> Row[Any] | None:
        """Execute ``statement`` and return the first row, or ``None``.

        More than one row is a programming error, not a data condition — every caller of this method
        queries by primary key — so it raises rather than silently taking the first.
        """
        rows = self.fetch_all(statement, params)
        if not rows:
            return None
        if len(rows) > 1:
            raise RepositoryError(
                f"{statement.id} was expected to match at most one row, matched {len(rows)}"
            )
        return rows[0]

    def fetch_scalar(
        self,
        statement: Statement,
        params: Mapping[str, Any] | None = None,
        *,
        default: Any = None,
    ) -> Any:
        """Execute ``statement`` and return the first column of the first row.

        ``default`` covers both no rows and a SQL ``NULL``. An empty ``SUM`` in SQLite is ``NULL``,
        and every caller here wants a number: requirement 8.5's exposure total for a customer with
        no
        credit is zero, not unknown.
        """
        row = self.fetch_one(statement, params)
        if row is None or row[0] is None:
            return default
        return row[0]


# ---------------------------------------------------------------- row mapping
def to_model[ModelT: DomainModel](
    model: type[ModelT],
    row: Row[Any],
    **overrides: Any,
) -> ModelT:
    """Validate a row into ``model``.

    ``overrides`` supplies the provenance columns a dependent table does not have of its own —
    ``source_system`` for ``contact_info`` or ``account``, for instance, taken from the aggregate
    root. See :mod:`c360.domain.models` for why the invariant is held at the model layer even though
    the schema does not carry the column everywhere.

    Validation is real validation, not construction: ``model_config`` sets ``extra="forbid"``, so a
    query selecting a column the model does not declare fails here rather than dropping it. That is
    what keeps a migration and its models from drifting apart quietly.
    """
    data: dict[str, Any] = dict(row._mapping)
    data.update(overrides)
    return model.model_validate(data)


def to_models[ModelT: DomainModel](
    model: type[ModelT],
    rows: Sequence[Row[Any]],
    **overrides: Any,
) -> tuple[ModelT, ...]:
    """:func:`to_model` over a sequence of rows."""
    return tuple(to_model(model, row, **overrides) for row in rows)


def optional_model[ModelT: DomainModel](
    model: type[ModelT],
    row: Row[Any] | None,
    **overrides: Any,
) -> ModelT | None:
    """:func:`to_model`, passing ``None`` through for a missing row."""
    return None if row is None else to_model(model, row, **overrides)
