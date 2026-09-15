"""The seeded random source and the shared drawing helpers (task 2.1).

One ``random.Random`` is threaded through every generator, per task 2.1. It is created once in
:meth:`GeneratorContext.create` and never reseeded, and no generator is permitted to import
:mod:`random` for itself — the module-level functions in :mod:`random` share a global instance whose
state depends on whatever else in the process has drawn from it, so a single call to
``random.choice`` anywhere in the package would make the output depend on import order.

Three non-obvious threats to byte-identical output, and what is done about them
------------------------------------------------------------------------------

**``hash()`` on a string is salted per process.** ``PYTHONHASHSEED`` is random by default, so
iterating a ``set`` of strings, or a ``dict`` keyed by strings built in non-deterministic order,
yields a different order in every process. Any such order that reaches the emitted rows makes the
database differ between two runs at the same seed. The rule in this package is: never iterate a
``set``; sort it, or keep a list alongside it. :func:`stable_hash` exists so the one place that
genuinely needs a hash — ``household.address_hash`` — uses SHA-256 rather than ``hash()``.

**Today's date is not a constant.** ``as_of_date`` appears on almost every table, so seeding it from
:func:`c360.domain.dates.today_iso` would mean the same seed produced a different database tomorrow.
The as-of date is therefore a fixed anchor, :data:`DEFAULT_AS_OF`, overridable per run. That also
makes the evaluation ground truth of design §15.1 stable: a panel pinned in Phase 11 keeps meaning
the same thing next month.

**Float accumulation is fine, but only if the operations are identical.** ``Random.choices`` with
weights accumulates floats. That is deterministic for a given seed and platform, so it is used
freely; what is avoided is deriving a *monetary* value from a float, which is
:mod:`c360.domain.money`'s rule, not this module's.

Draw order is load-bearing
--------------------------

Every helper here consumes a defined number of values from the stream, so adding a draw to an early
generator shifts every later one. That is expected and harmless — the output is still reproducible —
but it does mean the seeded dataset is not stable across code changes to the generator, only across
runs of the same code. Design §14.6 covers this by recording the code revision alongside the data
seed in every evaluation run record.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from random import Random
from typing import Final, TypeVar

from c360.domain.dates import to_iso
from c360.domain.money import Cents

T = TypeVar("T")

#: The as-of date every generated row is current at. Fixed rather than "today" — see the module
#: docstring. Chosen to sit at a month boundary so the 24-month transaction window is whole months.
DEFAULT_AS_OF: Final = date(2026, 9, 1)

#: Length of the transaction history, in months. Design §15: ~200 transactions over 24 months.
HISTORY_MONTHS: Final = 24

#: Source systems the rows are attributed to. Requirement 14.6 carries ``source_system`` on reads,
#: so the values have to be plausible and stable rather than a single placeholder.
SOURCE_CORE_BANKING: Final = "CORE_BANKING"
SOURCE_CRM: Final = "CRM"
SOURCE_CARDS: Final = "CARD_PLATFORM"
SOURCE_WEALTH: Final = "WEALTH_PLATFORM"
SOURCE_LOS: Final = "LOAN_ORIGINATION"
SOURCE_MDM: Final = "MDM"
SOURCE_CAMPAIGN: Final = "CAMPAIGN_MANAGER"

#: Percent scale for :meth:`GeneratorContext.chance`.
_FULL_PERCENT: Final = 100

#: Days per month used when stepping the synthetic calendar. Real month lengths are respected by
#: :func:`add_months`; this is only for coarse jitter inside a month.
_DAYS_IN_SHORT_MONTH: Final = 28

#: Last month of the year, named so :func:`add_months`'s year rollover is not a bare literal.
_DECEMBER: Final = 12


def stable_hash(*parts: str, length: int = 12) -> str:
    """Return a short, process-stable hex digest of ``parts``.

    SHA-256 rather than :func:`hash`, which is salted per process and would make
    ``household.address_hash`` — and therefore the derived household grouping of design §4.3 —
    differ
    between two runs at the same seed.

    Truncated because this is a grouping key, not a security control: it never protects a secret,
    and
    a collision would merge two synthetic households.
    """
    joined = "\u001f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]


def add_months(anchor: date, months: int) -> date:
    """Shift ``anchor`` by ``months``, clamping the day to the target month's length.

    ``timedelta`` cannot express a month, and naive day arithmetic drifts: 31 January plus one month
    has to be 28 February, not 3 March. Loan maturities and monthly transaction cycles both depend
    on
    this landing on a real calendar date, because every date column is checked with ``IS date(x)``
    and SQLite would silently normalize an impossible date into a different one.
    """
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    # The first of the following month, less a day, is the last day of this one. December rolls into
    # January of the next year.
    next_month_start = date(year + 1, 1, 1) if month == _DECEMBER else date(year, month + 1, 1)
    days_in_month = (next_month_start - timedelta(days=1)).day
    return date(year, month, min(anchor.day, days_in_month))


def month_starts(first: date, count: int) -> list[date]:
    """Return ``count`` consecutive month-start dates beginning at ``first``'s month."""
    start = first.replace(day=1)
    return [add_months(start, offset) for offset in range(count)]


@dataclass(slots=True)
class GeneratorContext:
    """The seeded random source, the as-of anchor, and the drawing helpers built on them.

    Constructed by :meth:`create`, then passed down through every generator. Mutable only in that
    the wrapped ``Random`` advances; nothing else on it changes during a run.
    """

    rng: Random
    as_of: date
    seed: int
    customer_count: int

    @classmethod
    def create(cls, *, count: int, seed: int, as_of: date = DEFAULT_AS_OF) -> GeneratorContext:
        """Build a context. The only place a ``Random`` is constructed in this package."""
        if count < 1:
            raise ValueError(f"count must be positive, got {count}")
        return cls(rng=Random(seed), as_of=as_of, seed=seed, customer_count=count)

    # ---------------------------------------------------------------- calendar
    @property
    def as_of_iso(self) -> str:
        """The as-of date as ``YYYY-MM-DD``, ready to bind."""
        return to_iso(self.as_of)

    @property
    def history_start(self) -> date:
        """First day of the transaction window: ``HISTORY_MONTHS`` before the as-of month."""
        return add_months(self.as_of.replace(day=1), -HISTORY_MONTHS)

    def history_months(self) -> list[date]:
        """The month-start dates of the transaction window, oldest first."""
        return month_starts(self.history_start, HISTORY_MONTHS)

    # ---------------------------------------------------------------- primitives
    def chance(self, percent: int) -> bool:
        """Whether an event with integer ``percent`` probability occurs.

        Integer percent rather than a float probability so no threshold comparison depends on a
        float literal round-tripping exactly.
        """
        if percent <= 0:
            return False
        if percent >= _FULL_PERCENT:
            return True
        return self.rng.randrange(_FULL_PERCENT) < percent

    def pick(self, options: Sequence[T]) -> T:
        """Choose one element uniformly.

        Takes a ``Sequence``, never a ``set``: see the module docstring on why set ordering must
        not be allowed to reach the emitted rows.
        """
        if not options:
            raise ValueError("pick requires a non-empty sequence")
        return options[self.rng.randrange(len(options))]

    def weighted(self, options: Sequence[tuple[T, int]]) -> T:
        """Choose one element by integer weight."""
        if not options:
            raise ValueError("weighted requires a non-empty sequence")
        total = sum(weight for _, weight in options)
        if total <= 0:
            raise ValueError("weighted requires the weights to sum to more than zero")
        target = self.rng.randrange(total)
        cumulative = 0
        for value, weight in options:
            cumulative += weight
            if target < cumulative:
                return value
        return options[-1][0]  # pragma: no cover - unreachable while weights are positive

    def sample(self, options: Sequence[T], size: int) -> list[T]:
        """Choose ``size`` distinct elements, preserving a deterministic order."""
        capped = max(0, min(size, len(options)))
        return self.rng.sample(list(options), capped)

    def shuffled(self, options: Sequence[T]) -> list[T]:
        """Return a shuffled copy."""
        items = list(options)
        self.rng.shuffle(items)
        return items

    def integer(self, bounds: tuple[int, int]) -> int:
        """Draw from an inclusive ``(low, high)`` range, tolerating an inverted one."""
        low, high = bounds
        if low > high:
            low, high = high, low
        return self.rng.randint(low, high)

    def cents(self, bounds: tuple[int, int], *, round_to: int = 1) -> Cents:
        """Draw a monetary amount from an inclusive cents range.

        ``round_to`` snaps the result to a multiple, so balances look like money rather than like
        random integers: a savings balance of $8,432.00 reads as real, $8,432.17 reads as generated.
        """
        value = self.integer(bounds)
        if round_to > 1:
            value = (value // round_to) * round_to
        return Cents(value)

    # ---------------------------------------------------------------- calendar draws
    def date_between(self, start: date, end: date) -> date:
        """Draw a date from an inclusive range."""
        if end < start:
            start, end = end, start
        span = (end - start).days
        return start + timedelta(days=self.rng.randint(0, span))

    def day_in_month(self, month_start: date, *, low: int = 1, high: int = 28) -> date:
        """Draw a day inside ``month_start``'s month.

        The default upper bound is 28 so the result exists in February without special-casing, which
        matters because every date column is validated with ``IS date(x)``.
        """
        capped_high = min(high, _DAYS_IN_SHORT_MONTH)
        floor = max(1, low)
        day = self.rng.randint(floor, max(floor, capped_high))
        return month_start.replace(day=day)

    def jitter_days(self, anchor: date, spread: int) -> date:
        """Shift ``anchor`` by up to ``spread`` days in either direction."""
        return anchor + timedelta(days=self.rng.randint(-spread, spread))

    # ---------------------------------------------------------------- identifiers
    @staticmethod
    def entity_id(prefix: str, index: int, *, width: int = 5) -> str:
        """Build a zero-padded identifier such as ``C-00042``.

        Zero-padded to a fixed width so the identifiers sort lexicographically in the same order
        they
        sort numerically. Several later phases order by ID as a tiebreaker, and ``C-10`` sorting
        before ``C-9`` would make those orderings look arbitrary in the UI.
        """
        return f"{prefix}-{index:0{width}d}"

    def digits(self, length: int) -> str:
        """Draw a string of ``length`` decimal digits, first digit never zero."""
        if length < 1:
            raise ValueError(f"length must be positive, got {length}")
        first = self.rng.randint(1, 9)
        rest = "".join(str(self.rng.randint(0, 9)) for _ in range(length - 1))
        return f"{first}{rest}"
