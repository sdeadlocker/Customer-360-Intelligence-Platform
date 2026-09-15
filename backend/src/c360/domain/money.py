"""Monetary and rate value types (task 1.5).

Design §1.1 sets the rule this module exists to enforce: *no `NUMERIC(18,2)`; REAL is binary
floating
point and drifts under summation*, so every monetary column is ``INTEGER`` minor units and every
rate
is ``INTEGER`` basis points, with conversion to ``Decimal`` deferred to the API boundary.

The reason for real types rather than a naming convention
---------------------------------------------------------

``_cents`` as a column suffix stops being protection the moment a value is in a Python variable. Two
mistakes then become indistinguishable to the reader and to the type checker: adding a dollar amount
to a cents amount, and adding a basis-point rate to a cents amount. Both are plain ``int + int``.
:class:`Cents` and :class:`Bps` make both a ``TypeError``.

Why they subclass ``int``
-------------------------

A wrapper class holding ``value: int`` would be the more fashionable choice, and it was rejected. A
``Cents`` that *is* an ``int`` binds directly as a SQLite parameter, comes back out of a row without
being rewrapped, works with :func:`sum`, and serializes without a custom encoder. A wrapper needs
``.value`` at every one of those boundaries, and unwrapping at a boundary is precisely where a raw
``int`` re-enters the system unlabelled.

Subclassing does mean the operators have to be narrowed deliberately, which is what most of this
module is. The narrowing is the feature:

* ``Cents + Cents`` is ``Cents``; ``Cents + int`` is a ``TypeError``.
* ``Cents * int`` is ``Cents`` (a quantity of money, scaled); ``Cents * Cents`` is a ``TypeError``,
  because square cents are not a thing.
* ``Cents / anything`` is a ``TypeError``. Division is where drift enters, so it is only available
  through :meth:`Cents.divide` and :meth:`Cents.apply_bps`, which take an explicit rounding mode.

Rounding, and why the default is not the same everywhere
--------------------------------------------------------

:meth:`Cents.ratio_bps` defaults to :attr:`Rounding.TRUNCATE`, while :meth:`Cents.apply_bps` and
:meth:`Cents.divide` default to :attr:`Rounding.HALF_EVEN`. That asymmetry is deliberate and load
bearing. Design §4.6 defines ``utilization_bps`` as ``balance_cents * 10000 / credit_limit_cents``
*evaluated in SQL*, and SQLite integer division truncates toward zero. Task 3.1 has to assert that
stored derived values equal freshly-computed ones, so the Python path has to round the way the SQL
path rounds or that test fails on values ending in a half. Money being *created* — interest, a fee,
a
share of an allocation — uses banker's rounding, which is the convention that does not bias a large
population of roundings upward.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Self

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema

#: Minor units per major unit. 100 for every currency this platform handles.
CENTS_PER_UNIT: Final = 100

#: Basis points per whole. 10,000 bps = 100% = a ratio of 1.
BPS_PER_UNIT: Final = 10_000

#: Decimal exponent for a cents value expressed in major units.
_MINOR_UNIT_EXPONENT: Final = -2


class MoneyConversionError(ValueError):
    """Raised when a value cannot be converted without losing precision.

    Distinct from ``TypeError``, which the operators raise for unit mistakes. This one means the
    units were right and the *precision* was not — a sub-cent input, or a non-finite Decimal.
    """


class Rounding(StrEnum):
    """How an inexact integer division resolves.

    Named rather than defaulted implicitly, because "which way does this round" is a question that
    should have an answer at the call site of anything producing money.
    """

    #: Round half to even, a.k.a. banker's rounding. The default for producing money.
    HALF_EVEN = "half_even"
    #: Round half away from zero. Conventional for interest and fee schedules.
    HALF_UP = "half_up"
    #: Truncate toward zero, matching SQLite's integer division. The default for ratios that must
    #: reconcile with a value computed in SQL.
    TRUNCATE = "truncate"


def _divide(numerator: int, denominator: int, rounding: Rounding) -> int:
    """Divide two integers to an integer result, exactly, under an explicit rounding mode.

    Implemented on integers rather than by going through :class:`Decimal` so there is no context
    precision to overflow and no intermediate binary float anywhere in the path.
    """
    if denominator == 0:
        raise ZeroDivisionError("cannot divide a monetary value by zero")

    # Normalize the sign onto the numerator so the rounding logic only handles a positive divisor.
    if denominator < 0:
        numerator, denominator = -numerator, -denominator

    if rounding is Rounding.TRUNCATE:
        # SQLite truncates toward zero; Python's `//` floors, which differs for negative numerators.
        quotient = abs(numerator) // denominator
        return -quotient if numerator < 0 else quotient

    # `divmod` floors, so the remainder is always in [0, denominator).
    quotient, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    if doubled > denominator:
        return quotient + 1
    if doubled < denominator:
        return quotient
    # Exactly half. `divmod` floored, so the tie is resolved by stepping up or staying put.
    if rounding is Rounding.HALF_UP:
        # Away from zero: a floored negative quotient is already the more-negative option.
        return quotient if numerator < 0 else quotient + 1
    return quotient + 1 if quotient % 2 else quotient


def _require_int(value: object, type_name: str) -> int:
    """Validate a constructor argument.

    ``bool`` is rejected explicitly. It is a subclass of ``int``, so ``Cents(True)`` would otherwise
    quietly produce one cent — and a boolean reaching a money constructor means a column was read
    from the wrong position.
    """
    if isinstance(value, bool):
        raise TypeError(f"{type_name} cannot be built from a bool")
    if not isinstance(value, int):
        raise TypeError(f"{type_name} requires an int, got {type(value).__name__}")
    return value


def _pydantic_int_schema(cls: type, handler: GetCoreSchemaHandler) -> CoreSchema:
    """Core schema for an ``int``-backed value type.

    Validation coerces to the value type so a model field declared ``Cents`` really holds
    :class:`Cents` after validation, not a bare ``int``. Serialization stays integral: converting to
    ``Decimal`` is the API response model's job (design §1.1 puts that at the boundary), and doing
    it here would put decimals into the audit log and the tool layer as well.
    """
    from pydantic_core import core_schema  # noqa: PLC0415 - avoids a module-import cycle

    del handler
    return core_schema.no_info_after_validator_function(
        cls,
        core_schema.int_schema(strict=False),
        serialization=core_schema.plain_serializer_function_ser_schema(
            int, return_schema=core_schema.int_schema()
        ),
    )


class Cents(int):
    """A monetary amount in minor units.

    Signed: a debit is negative and a credit is positive, matching ``txn.amount_cents`` in design
    §4.3.
    """

    __slots__ = ()

    def __new__(cls, value: int = 0) -> Self:
        return super().__new__(cls, _require_int(value, cls.__name__))

    # ---------------------------------------------------------------- construction
    @classmethod
    def from_decimal(
        cls,
        amount: Decimal | str | int,
        *,
        rounding: Rounding | None = None,
    ) -> Self:
        """Build from a major-unit amount, e.g. ``Decimal("1234.56")`` → ``123456``.

        Raises :class:`MoneyConversionError` on sub-cent precision unless ``rounding`` is given.
        Silently dropping a third decimal place is how a reconciliation break starts, so discarding
        precision has to be asked for.
        """
        value = Decimal(amount) if not isinstance(amount, Decimal) else amount
        if not value.is_finite():
            raise MoneyConversionError(f"cannot convert non-finite value {value!r} to cents")

        shifted = value.scaleb(-_MINOR_UNIT_EXPONENT)
        integral = shifted.to_integral_value(rounding="ROUND_DOWN")
        if shifted != integral:
            if rounding is None:
                raise MoneyConversionError(
                    f"{value} has sub-cent precision; pass rounding= to convert it deliberately"
                )
            # Re-express as an exact integer division so one rounding implementation governs.
            numerator, denominator = value.as_integer_ratio()
            return cls(_divide(numerator * CENTS_PER_UNIT, denominator, rounding))
        return cls(int(integral))

    # ---------------------------------------------------------------- boundary conversion
    def to_decimal(self) -> Decimal:
        """Return the amount in major units, exactly. The API boundary conversion of design §1.1."""
        return Decimal(int(self)).scaleb(_MINOR_UNIT_EXPONENT)

    # ---------------------------------------------------------------- arithmetic
    def __add__(self, other: Cents) -> Cents:  # type: ignore[override]
        if not isinstance(other, Cents):
            raise TypeError(f"cannot add {type(other).__name__} to Cents; wrap it in Cents first")
        return Cents(int(self) + int(other))

    def __radd__(self, other: Cents | int) -> Cents:
        # Reached for `0 + cents`, which is how `sum()` starts. Python prefers the reflected method
        # here because `Cents` is a subclass of `int` and overrides it, so `int.__add__` never gets
        # the chance to quietly degrade the result to a plain `int`.
        if isinstance(other, Cents):
            return self.__add__(other)
        # The additive identity is the one bare int whose units are unambiguous. Anything else is a
        # unit mistake, and raising rather than returning `NotImplemented` is deliberate: returning
        # it would let Python fall back to `int.__add__` and succeed.
        if other == 0:
            return Cents(int(self))
        raise TypeError(f"cannot add Cents to {type(other).__name__}; wrap it in Cents first")

    def __sub__(self, other: Cents) -> Cents:  # type: ignore[override]
        if not isinstance(other, Cents):
            raise TypeError(
                f"cannot subtract {type(other).__name__} from Cents; wrap it in Cents first"
            )
        return Cents(int(self) - int(other))

    def __rsub__(self, other: Cents) -> Cents:  # type: ignore[override]
        if not isinstance(other, Cents):
            raise TypeError(f"cannot subtract Cents from {type(other).__name__}")
        return Cents(int(other) - int(self))

    def __neg__(self) -> Cents:
        return Cents(-int(self))

    def __pos__(self) -> Cents:
        return Cents(int(self))

    def __abs__(self) -> Cents:
        return Cents(abs(int(self)))

    def __mul__(self, other: int) -> Cents:
        """Scale by a whole number. ``Cents * Cents`` is rejected: it has no meaning."""
        if isinstance(other, Cents):
            raise TypeError("cannot multiply Cents by Cents")
        if isinstance(other, bool) or not isinstance(other, int):
            raise TypeError(f"cannot multiply Cents by {type(other).__name__}")
        return Cents(int(self) * other)

    def __rmul__(self, other: int) -> Cents:
        return self.__mul__(other)

    def __truediv__(self, other: object) -> Cents:
        raise TypeError(
            "Cents does not support `/`: float division is where drift enters. "
            "Use Cents.divide(divisor, rounding=...) or Cents.ratio_bps(denominator)."
        )

    def __floordiv__(self, other: object) -> Cents:
        raise TypeError(
            "Cents does not support `//`: the rounding mode must be explicit. "
            "Use Cents.divide(divisor, rounding=...)."
        )

    def divide(self, divisor: int, *, rounding: Rounding = Rounding.HALF_EVEN) -> Cents:
        """Split into ``divisor`` parts, returning the size of one part."""
        return Cents(_divide(int(self), _require_int(divisor, "divisor"), rounding))

    # ---------------------------------------------------------------- rates
    def apply_bps(self, rate: Bps, *, rounding: Rounding = Rounding.HALF_EVEN) -> Cents:
        """Return ``self`` multiplied by a basis-point rate. 250 bps of 10000 cents is 250 cents."""
        if not isinstance(rate, Bps):
            raise TypeError(f"apply_bps requires Bps, got {type(rate).__name__}")
        return Cents(_divide(int(self) * int(rate), BPS_PER_UNIT, rounding))

    def ratio_bps(self, denominator: Cents, *, rounding: Rounding = Rounding.TRUNCATE) -> Bps:
        """Return ``self / denominator`` as basis points.

        This is design §4.6's ``utilization_bps`` and ``credit_utilization_bps``. Truncating by
        default keeps it equal to the same expression evaluated in SQLite, which is what task 3.1's
        stored-equals-computed assertion depends on.
        """
        if not isinstance(denominator, Cents):
            raise TypeError(f"ratio_bps requires Cents, got {type(denominator).__name__}")
        return Bps(_divide(int(self) * BPS_PER_UNIT, int(denominator), rounding))

    # ---------------------------------------------------------------- allocation
    def allocate(self, weights: Sequence[int]) -> tuple[Cents, ...]:
        """Split across ``weights`` so the parts sum back to exactly ``self``.

        The largest-remainder method: floor every share, then hand the leftover minor units out one
        at a time, largest remainder first. Rounding each share independently would lose or invent
        pennies, which matters for ``account_party.ownership_bps`` and ``beneficiary.share_bps``,
        where the shares of one balance have to reconcile to the balance.
        """
        if not weights:
            raise ValueError("allocate requires at least one weight")
        if any(weight < 0 for weight in weights):
            raise ValueError("allocate requires non-negative weights")
        total_weight = sum(weights)
        if total_weight == 0:
            raise ValueError("allocate requires the weights to sum to more than zero")

        amount = int(self)
        # Floor toward negative infinity for both signs, so the remainders handed out below are
        # always non-negative and the loop is sign-agnostic.
        shares = [(amount * weight) // total_weight for weight in weights]
        remainders = [(amount * weight) % total_weight for weight in weights]
        leftover = amount - sum(shares)

        # Ties broken by original position, so the result is deterministic — a requirement, not a
        # nicety, given the byte-identical-output test in task 2.1.
        order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
        for index in order[:leftover]:
            shares[index] += 1
        return tuple(Cents(share) for share in shares)

    # ---------------------------------------------------------------- presentation
    def __repr__(self) -> str:
        return f"Cents({int(self)})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        del source
        return _pydantic_int_schema(cls, handler)


class Bps(int):
    """A rate or percentage in basis points. 10,000 bps is 100%."""

    __slots__ = ()

    def __new__(cls, value: int = 0) -> Self:
        return super().__new__(cls, _require_int(value, cls.__name__))

    # ---------------------------------------------------------------- construction
    @classmethod
    def from_ratio(cls, ratio: Decimal | str | int, *, rounding: Rounding | None = None) -> Self:
        """Build from a ratio: ``Decimal("0.0525")`` → 525 bps."""
        return cls._from_scaled(ratio, BPS_PER_UNIT, rounding)

    @classmethod
    def from_percent(
        cls, percent: Decimal | str | int, *, rounding: Rounding | None = None
    ) -> Self:
        """Build from a percentage: ``Decimal("5.25")`` → 525 bps."""
        return cls._from_scaled(percent, BPS_PER_UNIT // 100, rounding)

    @classmethod
    def _from_scaled(
        cls,
        value: Decimal | str | int,
        scale: int,
        rounding: Rounding | None,
    ) -> Self:
        decimal_value = Decimal(value) if not isinstance(value, Decimal) else value
        if not decimal_value.is_finite():
            raise MoneyConversionError(f"cannot convert non-finite value {decimal_value!r} to bps")

        numerator, denominator = decimal_value.as_integer_ratio()
        scaled_numerator = numerator * scale
        if scaled_numerator % denominator:
            if rounding is None:
                raise MoneyConversionError(
                    f"{decimal_value} has sub-basis-point precision; "
                    "pass rounding= to convert it deliberately"
                )
            return cls(_divide(scaled_numerator, denominator, rounding))
        return cls(scaled_numerator // denominator)

    # ---------------------------------------------------------------- boundary conversion
    def to_ratio(self) -> Decimal:
        """Return the rate as a ratio: 525 bps → ``Decimal("0.0525")``."""
        return Decimal(int(self)) / Decimal(BPS_PER_UNIT)

    def to_percent(self) -> Decimal:
        """Return the rate as a percentage: 525 bps → ``Decimal("5.25")``."""
        return Decimal(int(self)) / Decimal(BPS_PER_UNIT // 100)

    # ---------------------------------------------------------------- arithmetic
    def __add__(self, other: Bps) -> Bps:  # type: ignore[override]
        if not isinstance(other, Bps):
            raise TypeError(f"cannot add {type(other).__name__} to Bps; wrap it in Bps first")
        return Bps(int(self) + int(other))

    def __radd__(self, other: Bps | int) -> Bps:
        if isinstance(other, Bps):
            return self.__add__(other)
        if other == 0:
            return Bps(int(self))
        raise TypeError(f"cannot add Bps to {type(other).__name__}; wrap it in Bps first")

    def __sub__(self, other: Bps) -> Bps:  # type: ignore[override]
        if not isinstance(other, Bps):
            raise TypeError(
                f"cannot subtract {type(other).__name__} from Bps; wrap it in Bps first"
            )
        return Bps(int(self) - int(other))

    def __neg__(self) -> Bps:
        return Bps(-int(self))

    def __abs__(self) -> Bps:
        return Bps(abs(int(self)))

    def __mul__(self, other: int) -> Bps:
        if isinstance(other, Bps):
            raise TypeError("cannot multiply Bps by Bps")
        if isinstance(other, bool) or not isinstance(other, int):
            raise TypeError(f"cannot multiply Bps by {type(other).__name__}")
        return Bps(int(self) * other)

    def __rmul__(self, other: int) -> Bps:
        return self.__mul__(other)

    # ---------------------------------------------------------------- presentation
    def __repr__(self) -> str:
        return f"Bps({int(self)})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        del source
        return _pydantic_int_schema(cls, handler)


#: The additive identity, provided so callers can seed a fold without constructing one inline.
ZERO_CENTS: Final = Cents(0)
ZERO_BPS: Final = Bps(0)
