"""Money, rate and date conversion tests (task 1.5).

The task asks for a "unit test proving no float drift across aggregate paths (sum of 10k values)".
:func:`test_no_float_drift_over_ten_thousand_values` is that test, and it is written to fail if the
implementation were ever changed to use floats — it asserts the integer path is exact *and* that the
float path it replaces is not, so the test is evidence rather than a tautology.

The rest is about the second failure mode design §1.1 is worried about: not arithmetic drift, but
unit
confusion. Cents added to dollars, basis points added to cents, a probability multiplied by a
balance.
Each of those is a plain ``int + int`` without these types, and each is asserted to raise here.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from c360.domain.dates import (
    InvalidIsoDateError,
    from_iso,
    is_iso_date,
    optional_from_iso,
    optional_to_iso,
    to_iso,
    to_iso_utc,
    today_iso,
)
from c360.domain.money import (
    BPS_PER_UNIT,
    CENTS_PER_UNIT,
    ZERO_BPS,
    ZERO_CENTS,
    Bps,
    Cents,
    MoneyConversionError,
    Rounding,
)

# ================================================================ the drift proof


def test_no_float_drift_over_ten_thousand_values() -> None:
    """Sum 10,000 amounts exactly, and show the float equivalent does not manage it.

    The seeded generator is used so the failure is reproducible rather than a flake, and the values
    span both signs because ``txn.amount_cents`` is signed.
    """
    rng = random.Random(1_759)
    amounts = [Cents(rng.randint(-5_000_000, 5_000_000)) for _ in range(10_000)]

    total = sum(amounts)
    assert isinstance(total, Cents), "sum() must preserve the type, not degrade to int"

    # Exact by construction: integer addition cannot lose information.
    assert int(total) == sum(int(amount) for amount in amounts)

    # The same total via Decimal major units agrees to the cent.
    assert total.to_decimal() == sum(amount.to_decimal() for amount in amounts)

    # And the float path a REAL column would have forced does not.
    as_float = 0.0
    for amount in amounts:
        as_float += int(amount) / CENTS_PER_UNIT
    assert as_float != float(total.to_decimal()), (
        "the float accumulation happened to be exact for this seed; "
        "the test no longer demonstrates what it claims"
    )
    assert abs(Decimal(repr(as_float)) - total.to_decimal()) < Decimal("0.01")


def test_repeated_aggregation_paths_agree() -> None:
    """Grouped then summed must equal summed directly, which is what derived values depend on."""
    rng = random.Random(7)
    amounts = [Cents(rng.randint(-999_999, 999_999)) for _ in range(10_000)]

    direct = sum(amounts)
    buckets: dict[int, list[Cents]] = {}
    for index, amount in enumerate(amounts):
        buckets.setdefault(index % 37, []).append(amount)
    grouped = sum((sum(bucket) for bucket in buckets.values()), start=ZERO_CENTS)

    assert direct == grouped


# ================================================================ Cents construction
def test_zero_default_and_constants() -> None:
    assert Cents() == 0
    assert Cents(0) == ZERO_CENTS
    assert Bps(0) == ZERO_BPS


@pytest.mark.parametrize(
    ("major", "expected"),
    [
        ("0.00", 0),
        ("1.00", 100),
        ("1234.56", 123_456),
        ("-99.99", -9_999),
        ("0.01", 1),
        ("-0.01", -1),
        ("12345678901234.56", 1_234_567_890_123_456),
    ],
)
def test_from_decimal_exact(major: str, expected: int) -> None:
    assert Cents.from_decimal(Decimal(major)) == expected


def test_from_decimal_accepts_str_and_int() -> None:
    assert Cents.from_decimal("10.50") == 1050
    assert Cents.from_decimal(7) == 700


def test_from_decimal_rejects_sub_cent_precision_by_default() -> None:
    """Silently dropping a third decimal place is how a reconciliation break starts."""
    with pytest.raises(MoneyConversionError, match="sub-cent precision"):
        Cents.from_decimal(Decimal("1.005"))


@pytest.mark.parametrize(
    ("rounding", "expected"),
    [(Rounding.HALF_EVEN, 100), (Rounding.HALF_UP, 101), (Rounding.TRUNCATE, 100)],
)
def test_from_decimal_with_explicit_rounding(rounding: Rounding, expected: int) -> None:
    assert Cents.from_decimal(Decimal("1.005"), rounding=rounding) == expected


def test_from_decimal_rejects_non_finite() -> None:
    for value in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(MoneyConversionError, match="non-finite"):
            Cents.from_decimal(Decimal(value))


def test_to_decimal_is_exact_and_round_trips() -> None:
    assert Cents(123_456).to_decimal() == Decimal("1234.56")
    assert Cents(-1).to_decimal() == Decimal("-0.01")
    for raw in (0, 1, -1, 99, 100, 101, -12_345_678):
        assert Cents.from_decimal(Cents(raw).to_decimal()) == raw


def test_bool_is_rejected_as_a_constructor_argument() -> None:
    """``bool`` is an ``int`` subclass, so ``Cents(True)`` would otherwise be one cent."""
    with pytest.raises(TypeError, match="cannot be built from a bool"):
        Cents(True)
    with pytest.raises(TypeError, match="cannot be built from a bool"):
        Bps(False)


def test_float_is_rejected_as_a_constructor_argument() -> None:
    with pytest.raises(TypeError, match="requires an int"):
        Cents(1.5)  # type: ignore[arg-type]


# ================================================================ Cents arithmetic
def test_addition_and_subtraction_preserve_the_type() -> None:
    assert Cents(300) + Cents(200) == Cents(500)
    assert isinstance(Cents(300) + Cents(200), Cents)
    assert Cents(300) - Cents(500) == Cents(-200)
    assert isinstance(-Cents(5), Cents)
    assert abs(Cents(-5)) == Cents(5)
    assert isinstance(+Cents(5), Cents)


def test_sum_of_an_empty_and_single_sequence() -> None:
    assert sum([], ZERO_CENTS) == Cents(0)
    assert sum([Cents(42)]) == Cents(42)
    assert isinstance(sum([Cents(42)]), Cents)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: Cents(1) + 1, id="cents plus int"),
        pytest.param(lambda: 1 + Cents(1), id="int plus cents"),
        pytest.param(lambda: Cents(1) - 1, id="cents minus int"),
        pytest.param(lambda: 1 - Cents(1), id="int minus cents"),
        pytest.param(lambda: Cents(1) + Bps(1), id="cents plus bps"),
        pytest.param(lambda: Bps(1) + Cents(1), id="bps plus cents"),
        pytest.param(lambda: Cents(1) + 1.0, id="cents plus float"),
    ],
)
def test_mixed_unit_arithmetic_is_rejected(operation: object) -> None:
    """The unit mistakes design §1.1 cares about, each of which is plain ``int + int`` untyped."""
    assert callable(operation)
    with pytest.raises(TypeError):
        operation()


def test_zero_is_the_one_bare_int_addition_accepts() -> None:
    """``sum()`` starts at the literal 0; the additive identity has unambiguous units."""
    assert 0 + Cents(7) == Cents(7)
    assert isinstance(0 + Cents(7), Cents)


def test_scaling_by_a_whole_number() -> None:
    assert Cents(150) * 3 == Cents(450)
    assert 3 * Cents(150) == Cents(450)
    assert isinstance(Cents(150) * 3, Cents)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: Cents(2) * Cents(3), id="cents times cents"),
        pytest.param(lambda: Cents(2) * 1.5, id="cents times float"),
        pytest.param(lambda: Cents(2) * True, id="cents times bool"),
        pytest.param(lambda: Bps(2) * Bps(3), id="bps times bps"),
        pytest.param(lambda: Bps(2) * 0.5, id="bps times float"),
    ],
)
def test_meaningless_multiplication_is_rejected(operation: object) -> None:
    assert callable(operation)
    with pytest.raises(TypeError):
        operation()


def test_division_operators_are_unavailable() -> None:
    """Division needs an explicit rounding mode, so the operators are closed off entirely."""
    with pytest.raises(TypeError, match="does not support `/`"):
        Cents(4) / 2
    with pytest.raises(TypeError, match="does not support `//`"):
        Cents(4) // 2


@pytest.mark.parametrize(
    ("numerator", "divisor", "rounding", "expected"),
    [
        (5, 2, Rounding.HALF_EVEN, 2),
        (7, 2, Rounding.HALF_EVEN, 4),
        (-5, 2, Rounding.HALF_EVEN, -2),
        (-7, 2, Rounding.HALF_EVEN, -4),
        (5, 2, Rounding.HALF_UP, 3),
        (-5, 2, Rounding.HALF_UP, -3),
        (5, 2, Rounding.TRUNCATE, 2),
        (-5, 2, Rounding.TRUNCATE, -2),
        (100, 3, Rounding.HALF_EVEN, 33),
        (-100, 3, Rounding.HALF_EVEN, -33),
        (10, -4, Rounding.HALF_EVEN, -2),
    ],
)
def test_divide_rounding_modes(
    numerator: int, divisor: int, rounding: Rounding, expected: int
) -> None:
    assert Cents(numerator).divide(divisor, rounding=rounding) == expected


def test_divide_by_zero_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        Cents(100).divide(0)


# ================================================================ rates
def test_apply_bps() -> None:
    assert Cents(10_000).apply_bps(Bps(250)) == Cents(250)
    assert Cents(1_000_000).apply_bps(Bps(BPS_PER_UNIT)) == Cents(1_000_000)
    assert Cents(1_000_000).apply_bps(ZERO_BPS) == ZERO_CENTS
    assert isinstance(Cents(10_000).apply_bps(Bps(250)), Cents)


def test_apply_bps_requires_bps() -> None:
    with pytest.raises(TypeError, match="requires Bps"):
        Cents(100).apply_bps(250)  # type: ignore[arg-type]


def test_ratio_bps_requires_cents() -> None:
    with pytest.raises(TypeError, match="requires Cents"):
        Cents(100).ratio_bps(200)  # type: ignore[arg-type]


def test_ratio_bps_is_design_4_6_utilization() -> None:
    """`utilization_bps = balance_cents * 10000 / credit_limit_cents`, design §4.6."""
    assert Cents(250_000).ratio_bps(Cents(1_000_000)) == Bps(2_500)
    assert Cents(1_000_000).ratio_bps(Cents(1_000_000)) == Bps(BPS_PER_UNIT)
    assert isinstance(Cents(1).ratio_bps(Cents(3)), Bps)


@pytest.mark.parametrize(
    ("balance", "limit"),
    [
        (12_345, 100_000),
        (99_999, 100_000),
        (1, 3),
        (7, 3),
        (-5, 2),
        (250_000, 1_000_000),
        (999_999_999, 1_000_000_007),
    ],
)
def test_ratio_bps_matches_sqlite_integer_division(balance: int, limit: int) -> None:
    """The reason ``ratio_bps`` truncates by default.

    Design §4.6 evaluates ``utilization_bps`` in SQL, and task 3.1 asserts stored derived values
    equal
    freshly-computed ones. If Python rounded and SQLite truncated, that assertion would fail on
    every
    value landing on a half — so the two implementations are pinned to each other here.
    """
    connection = sqlite3.connect(":memory:")
    try:
        in_sql = connection.execute("SELECT ? * 10000 / ?", (balance, limit)).fetchone()[0]
    finally:
        connection.close()
    assert int(Cents(balance).ratio_bps(Cents(limit))) == in_sql


def test_bps_conversions() -> None:
    assert Bps.from_ratio(Decimal("0.0525")) == Bps(525)
    assert Bps.from_percent(Decimal("5.25")) == Bps(525)
    assert Bps(525).to_ratio() == Decimal("0.0525")
    assert Bps(525).to_percent() == Decimal("5.25")
    assert Bps.from_percent(Decimal("100")) == Bps(BPS_PER_UNIT)


def test_bps_rejects_sub_basis_point_precision() -> None:
    with pytest.raises(MoneyConversionError, match="sub-basis-point precision"):
        Bps.from_ratio(Decimal("0.052555"))
    assert Bps.from_ratio(Decimal("0.052555"), rounding=Rounding.HALF_UP) == Bps(526)


def test_bps_rejects_non_finite() -> None:
    with pytest.raises(MoneyConversionError, match="non-finite"):
        Bps.from_ratio(Decimal("NaN"))


def test_bps_arithmetic() -> None:
    assert Bps(100) + Bps(50) == Bps(150)
    assert Bps(100) - Bps(150) == Bps(-50)
    assert abs(Bps(-50)) == Bps(50)
    assert -Bps(50) == Bps(-50)
    assert Bps(100) * 3 == Bps(300)
    assert 3 * Bps(100) == Bps(300)
    assert sum([Bps(10), Bps(20)]) == Bps(30)
    with pytest.raises(TypeError):
        Bps(1) - 1
    with pytest.raises(TypeError):
        Bps(1) + 1


# ================================================================ allocation
def test_allocate_preserves_the_total() -> None:
    """No lost or invented pennies, which is what ``share_bps`` reconciliation needs."""
    shares = Cents(100).allocate([1, 1, 1])
    assert shares == (Cents(34), Cents(33), Cents(33))
    assert sum(shares) == Cents(100)


def test_allocate_by_weights() -> None:
    shares = Cents(10_000).allocate([5_000, 3_000, 2_000])
    assert shares == (Cents(5_000), Cents(3_000), Cents(2_000))
    assert sum(shares) == Cents(10_000)


def test_allocate_handles_negative_totals() -> None:
    shares = Cents(-100).allocate([1, 1, 1])
    assert sum(shares) == Cents(-100)
    assert all(isinstance(share, Cents) for share in shares)


def test_allocate_is_deterministic() -> None:
    """Task 2.1 requires two seeded runs to produce byte-identical databases."""
    for _ in range(5):
        assert Cents(1_000).allocate([1, 1, 1, 1, 1, 1, 7]) == Cents(1_000).allocate(
            [1, 1, 1, 1, 1, 1, 7]
        )


def test_allocate_tolerates_a_zero_weight() -> None:
    shares = Cents(100).allocate([1, 0, 1])
    assert shares[1] == ZERO_CENTS
    assert sum(shares) == Cents(100)


@pytest.mark.parametrize(
    ("weights", "message"),
    [
        ([], "at least one weight"),
        ([1, -1], "non-negative"),
        ([0, 0], "more than zero"),
    ],
)
def test_allocate_rejects_bad_weights(weights: list[int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Cents(100).allocate(weights)


def test_repr_is_unambiguous() -> None:
    assert repr(Cents(-5)) == "Cents(-5)"
    assert repr(Bps(250)) == "Bps(250)"


# ================================================================ pydantic integration
def test_value_types_work_as_pydantic_fields() -> None:
    """Repositories validate rows into models whose money fields are these types."""
    from pydantic import BaseModel  # noqa: PLC0415

    class Row(BaseModel):
        balance_cents: Cents
        rate_bps: Bps

    # Values arrive from SQLite as plain ints and must come out as the value types.
    model = Row.model_validate({"balance_cents": 123_456, "rate_bps": 525})
    assert isinstance(model.balance_cents, Cents)
    assert isinstance(model.rate_bps, Bps)
    assert model.balance_cents.to_decimal() == Decimal("1234.56")

    # Serialization stays integral: decimal conversion is the API boundary's job, not the type's.
    assert model.model_dump() == {"balance_cents": 123_456, "rate_bps": 525}
    assert model.model_dump_json() == '{"balance_cents":123456,"rate_bps":525}'


# ================================================================ dates
@pytest.mark.parametrize("text", ["2024-01-15", "1999-12-31", "2000-02-29", "2026-09-13"])
def test_from_iso_accepts_extended_format(text: str) -> None:
    assert to_iso(from_iso(text)) == text


@pytest.mark.parametrize(
    "text",
    [
        "2024-1-5",  # not zero-padded
        "20240115",  # basic format: date.fromisoformat accepts this, the CHECK constraint does not
        "2024-W03-1",  # ISO week date, likewise
        "2024-01-15T09:00:00",  # timestamp
        "2024-01-15 ",
        " 2024-01-15",
        "15/01/2024",
        "2024-02-30",  # well-formed, not a real date
        "2023-02-29",
        "2024-13-01",
        "",
        "not-a-date",
    ],
)
def test_from_iso_rejects_everything_else(text: str) -> None:
    """Deliberately tighter than :func:`date.fromisoformat`, to match the ``CHECK`` constraint."""
    with pytest.raises(InvalidIsoDateError):
        from_iso(text)


def test_parser_is_no_looser_than_the_database(tmp_path: object) -> None:
    """The Python parser and the SQL constraint must agree on every candidate, in both directions.

    This is the assertion that keeps the two definitions from drifting: a value the parser accepts
    must be storable, and a value it rejects must be rejected by the constraint too.
    """
    candidates = [
        "2024-01-15",
        "2000-02-29",
        "2024-1-5",
        "20240115",
        "2024-W03-1",
        "2024-01-15T09:00:00",
        "2024-02-30",
        "2023-02-29",
        "2024-13-01",
        "",
        "not-a-date",
    ]
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE t (d TEXT NOT NULL CHECK (d IS date(d)))",
        )
        for candidate in candidates:
            python_accepts = is_iso_date(candidate)
            try:
                connection.execute("INSERT INTO t (d) VALUES (?)", (candidate,))
                sqlite_accepts = True
            except sqlite3.IntegrityError:
                sqlite_accepts = False
            assert (
                python_accepts == sqlite_accepts
            ), f"{candidate!r}: python={python_accepts} sqlite={sqlite_accepts}"
    finally:
        connection.close()


def test_from_iso_rejects_non_strings() -> None:
    with pytest.raises(InvalidIsoDateError, match="requires a str"):
        from_iso(20240115)  # type: ignore[arg-type]


def test_to_iso_rejects_datetimes() -> None:
    """Truncating a datetime silently picks a time zone, so the caller has to choose."""
    with pytest.raises(InvalidIsoDateError, match="not a datetime"):
        to_iso(datetime(2024, 1, 15, 9, 0, tzinfo=UTC))
    with pytest.raises(InvalidIsoDateError, match="requires a date"):
        to_iso("2024-01-15")  # type: ignore[arg-type]


def test_to_iso_utc_makes_the_zone_choice_explicit() -> None:
    # 00:30 on the 16th in UTC+2 is still the 15th in UTC.
    aware = datetime(2024, 1, 16, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    assert to_iso_utc(aware) == "2024-01-15"
    # A naive datetime is treated as UTC rather than as local time.
    assert to_iso_utc(datetime(2024, 1, 15, 23, 59)) == "2024-01-15"
    with pytest.raises(InvalidIsoDateError, match="requires a datetime"):
        to_iso_utc(date(2024, 1, 15))  # type: ignore[arg-type]


def test_optional_converters_pass_none_through() -> None:
    """Requirement 4.7 distinguishes "no value" from a blank, so ``None`` has to survive."""
    assert optional_to_iso(None) is None
    assert optional_from_iso(None) is None
    assert optional_to_iso(date(2024, 1, 15)) == "2024-01-15"
    assert optional_from_iso("2024-01-15") == date(2024, 1, 15)


def test_today_iso_is_a_utc_date() -> None:
    value = today_iso()
    assert is_iso_date(value)
    assert from_iso(value) == datetime.now(UTC).date()


def test_is_iso_date_never_raises() -> None:
    assert is_iso_date("2024-01-15")
    for bad in (None, 20240115, "", "2024-1-5", "2024-02-30", object()):
        assert not is_iso_date(bad)


# ================================================================ reflected operator paths
# `Cents` and `Bps` subclass `int`, so Python resolves `int op Cents` through the *reflected* method
# on the subclass. That routing is easy to get subtly wrong and easy to leave untested, because the
# forward operator tests pass either way — so each reflected path is exercised explicitly.


def test_reflected_addition_of_two_value_types() -> None:
    """``Cents.__radd__`` reached with a ``Cents`` on the left, via an explicit call.

    Python normally resolves this through ``__add__``; the reflected branch runs when the left
    operand's own ``__add__`` returns ``NotImplemented``, and it has to agree with the forward one.
    """
    assert Cents(2).__radd__(Cents(3)) == Cents(5)
    assert Bps(2).__radd__(Bps(3)) == Bps(5)


def test_reflected_subtraction() -> None:
    assert Cents(3).__rsub__(Cents(10)) == Cents(7)
    assert isinstance(Cents(3).__rsub__(Cents(10)), Cents)
    with pytest.raises(TypeError, match="cannot subtract Cents from int"):
        Cents(3).__rsub__(10)  # type: ignore[operator]


def test_reflected_addition_rejects_a_bare_non_zero_int() -> None:
    """Raising, not returning ``NotImplemented``.

    Returning ``NotImplemented`` would let Python fall back to ``int.__add__`` and quietly
    succeed with a plain ``int`` — losing both the type and the unit check.
    """
    with pytest.raises(TypeError, match="cannot add Cents to int"):
        Cents(1).__radd__(5)
    with pytest.raises(TypeError, match="cannot add Bps to int"):
        Bps(1).__radd__(5)
    # And through the operator, which is the path that actually matters.
    with pytest.raises(TypeError):
        _ = 5 + Cents(1)
    with pytest.raises(TypeError):
        _ = 5 + Bps(1)
