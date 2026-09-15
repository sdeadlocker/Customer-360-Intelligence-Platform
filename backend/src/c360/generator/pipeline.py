"""Generation order and the seeding entry point (tasks 2.1, 2.8).

Design §15 fixes the generation order, and three of the dependencies in it are not obvious from the
table structure. They are the reason this module exists as an explicit sequence rather than as a
collection of stages that could run in any order:

1. **Life events are planned after products, before transactions.** After products, so a
   ``HOME_PURCHASE`` can be anchored to a mortgage that exists rather than asserted against holdings
   that contradict it. Before transactions, so the transaction stage can emit the corroborating
   debits
   design §15 requires. See :mod:`c360.generator.milestones`.
2. **Life-event rows are emitted after transactions.** The transaction stage appends transaction IDs
to
   each event's ``signals``, which is the citation requirement 7.3 renders. Emitting the
   ``life_event``
   row first would write an empty citation list.
3. **Profiles are emitted last.** ``financial_profile`` aggregates the accounts and assets that
earlier
   stages produced, and ``monthly_expense_cents`` comes from the transaction stage. It carries
   ``CHECK (net_worth_cents = total_assets_cents - total_liabilities_cents)``, so it cannot be
   written
   from anything but the real totals.

Note that this is *generation* order, which is not the same as *load* order — see
:data:`c360.generator.tables.LOAD_ORDER`, which puts ``asset`` before ``loan`` so a collateralized
loan
has something to reference.

Overwriting is opt-in
---------------------

:func:`seed` refuses to overwrite an existing database unless ``force=True``. A seeded database is
cheap
to rebuild, but it is also what a developer's running API, open notebook and half-finished debugging
session are all pointed at, and silently replacing it mid-session is a surprising amount of damage
for a
command whose name sounds additive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from c360.core.logging import get_logger
from c360.data.migrations import upgrade_database
from c360.generator.cohorts import Cohort
from c360.generator.context import DEFAULT_AS_OF, GeneratorContext
from c360.generator.emit import emit_core
from c360.generator.events import generate_events
from c360.generator.loader import LoadReport, load
from c360.generator.milestones import plan_life_events
from c360.generator.network import generate_network
from c360.generator.people import plan_people
from c360.generator.plan import Population
from c360.generator.products import plan_products
from c360.generator.profiles import generate_profiles
from c360.generator.tables import Dataset
from c360.generator.transactions import generate_transactions

_logger = get_logger(__name__)

#: Sidecar files WAL mode creates. Removed alongside the database so a rebuild cannot inherit stale
#: pages from a previous run — which would also break the byte-identical guarantee of task 2.1.
_SIDECAR_SUFFIXES = ("-wal", "-shm")


class DatabaseExistsError(FileExistsError):
    """Raised when seeding would overwrite an existing database without ``force``."""


@dataclass(frozen=True, slots=True)
class SeedReport:
    """The outcome of a seed run."""

    database: Path
    seed: int
    as_of: date
    customer_count: int
    cohort_counts: dict[Cohort, int]
    load: LoadReport

    @property
    def row_counts(self) -> dict[str, int]:
        return self.load.row_counts

    def summary_lines(self) -> list[str]:
        """Human-readable summary, for the CLI."""
        lines = [
            f"database      {self.database}",
            f"seed          {self.seed}",
            f"as-of         {self.as_of.isoformat()}",
            f"customers     {self.customer_count}",
            f"rows          {self.load.total_rows:,}",
            f"elapsed       {self.load.elapsed_seconds:.2f}s",
            "",
            "cohorts:",
        ]
        lines.extend(f"  {cohort.value:<16}{count}" for cohort, count in self.cohort_counts.items())
        lines.append("")
        lines.append("tables:")
        lines.extend(f"  {name:<24}{count:>9,}" for name, count in self.row_counts.items() if count)
        return lines


def generate(*, count: int, seed: int, as_of: date = DEFAULT_AS_OF) -> tuple[Population, Dataset]:
    """Run every generation stage and return the plan and the staged rows.

    Separated from :func:`seed` so tests can assert against the generated data without touching a
    filesystem, and so the Phase 11 ground-truth export can read the plan rather than re-deriving
    labels
    from the database.
    """
    ctx = GeneratorContext.create(count=count, seed=seed, as_of=as_of)

    # ---------------------------------------------------------------- planning
    population = plan_people(ctx)
    plan_products(ctx, population)
    plan_life_events(ctx, population)

    # ---------------------------------------------------------------- emission
    dataset = Dataset()
    emit_core(ctx, population, dataset)
    # Fills life-event signals and each customer's monthly expense, so it precedes both consumers.
    generate_transactions(ctx, population, dataset)
    generate_network(ctx, population, dataset)
    generate_events(ctx, population, dataset)
    generate_profiles(ctx, population, dataset)

    _logger.info(
        "dataset generated",
        extra={
            "customers": len(population.customers),
            "households": len(population.households),
            "rows": dataset.total_rows(),
            "seed": seed,
        },
    )
    return population, dataset


def seed(
    *,
    database: Path,
    count: int,
    seed: int,
    as_of: date = DEFAULT_AS_OF,
    force: bool = False,
) -> SeedReport:
    """Migrate ``database``, generate a dataset and load it.

    Args:
        database: Target SQLite file.
        count: Number of customers.
        seed: Random seed. The same seed and count produce a byte-identical database.
        as_of: The date every row is current at. Fixed rather than "today" so the output is stable
            across days.
        force: Replace an existing database. Without it, an existing file is an error.

    Raises:
        DatabaseExistsError: ``database`` exists and ``force`` is false.
    """
    if database.exists():
        if not force:
            raise DatabaseExistsError(
                f"{database} already exists. Pass force=True (or --force on the CLI) to replace it."
            )
        database.unlink()
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = database.with_name(database.name + suffix)
            if sidecar.exists():
                sidecar.unlink()

    upgrade_database(database)
    population, dataset = generate(count=count, seed=seed, as_of=as_of)
    report = load(dataset, database)

    return SeedReport(
        database=database,
        seed=seed,
        as_of=as_of,
        customer_count=len(population.customers),
        cohort_counts=population.cohort_counts(),
        load=report,
    )
