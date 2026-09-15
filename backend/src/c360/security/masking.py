"""The value transforms behind each masking mode (task 4.4).

Design §7.2 defines four modes but says what each *means* only in shorthand — ``PARTIAL`` is
"last-4 / year-only / city-only", ``BAND`` is "a coarse band instead of a numeric value". Those are
not one transform each: a partial account number is its last four digits, a partial date of birth
is its year, a partial address is its city. So a field declares not just its :class:`FieldGroup`
(which decides *whether* it is masked, from the matrix) but a :class:`MaskStyle` (which decides
*how* the PARTIAL and BAND forms are rendered).

Every transform is total and null-safe: masking a value that is already ``None`` yields ``None``,
and the HIDDEN mode is handled by the serializer dropping the key entirely rather than by a
transform here — a hidden field must be *absent* from the payload (requirement 12.4), not present
with a sentinel.
"""

from __future__ import annotations

from enum import StrEnum, auto
from typing import Final


class MaskStyle(StrEnum):
    """How the PARTIAL and BAND forms of a field are rendered.

    The style is a property of the *field*, not the role: the matrix decides that Contact Center
    gets PARTIAL on a date of birth, and the style decides that PARTIAL on a date of birth is the
    year. A field masked FULL or HIDDEN never consults its style.
    """

    #: Free text with no partial form; PARTIAL falls back to HIDDEN-like redaction.
    TEXT = auto()
    #: An account/loan/card identifier: PARTIAL keeps the last four characters.
    LAST4 = auto()
    #: An ISO date: PARTIAL keeps the year only.
    YEAR_ONLY = auto()
    #: A postal address: PARTIAL keeps the city (handled at the model level, see the field map).
    CITY_ONLY = auto()
    #: A monetary amount in integer cents: BAND maps it to a coarse labelled range.
    CURRENCY = auto()
    #: A 0-100 or bureau-style score: BAND maps it to a labelled band.
    SCORE = auto()
    #: A VIN: PARTIAL keeps the last four.
    VIN = auto()
    #: A person or entity name: PARTIAL keeps the first name / first token only.
    NAME = auto()


_REDACTED: Final = "***"

#: A value shorter than this cannot be shown "last four" without revealing the whole thing.
_MIN_LAST4_LEN: Final = 5

#: A four-digit year needs at least this many leading characters of an ISO date.
_YEAR_LEN: Final = 4


def partial_last4(value: str | None) -> str | None:
    """``**1234`` - keep the last four characters of an identifier.

    Anything shorter than five characters is fully redacted rather than partly revealed, so a
    four-digit value is not returned whole under a "partial" rule.
    """
    if value is None:
        return None
    stripped = value.strip()
    if len(stripped) < _MIN_LAST4_LEN:
        return _REDACTED
    return f"**{stripped[-4:]}"


def partial_year(iso_date: str | None) -> str | None:
    """``1982`` - keep the year of an ISO-8601 date, dropping month and day."""
    if iso_date is None:
        return None
    return iso_date[:_YEAR_LEN] if len(iso_date) >= _YEAR_LEN else _REDACTED


def partial_name(value: str | None) -> str | None:
    """``Renata ***`` - keep the first token of a name, redacting the rest."""
    if value is None:
        return None
    tokens = value.split()
    if not tokens:
        return _REDACTED
    return f"{tokens[0]} {_REDACTED}" if len(tokens) > 1 else tokens[0]


def partial_vin(value: str | None) -> str | None:
    """Keep the last four of a VIN, like any other sensitive identifier."""
    return partial_last4(value)


def redact(_value: object) -> str:
    """Full redaction for a PARTIAL rule on a field with no meaningful partial form."""
    return _REDACTED


# ---------------------------------------------------------------- banding
#: Currency bands in integer cents, low bound inclusive. Chosen to be coarse enough that a band
#: reveals scale without revealing a figure (design §7.2: "a coarse band instead of a numeric
#: value"). Labels are dollars for readability at the boundary; the underlying value never leaves.
_CURRENCY_BANDS: Final[tuple[tuple[int, str], ...]] = (
    (0, "$0"),
    (1, "<$1K"),
    (100_000, "$1K-$10K"),
    (1_000_000, "$10K-$100K"),
    (10_000_000, "$100K-$1M"),
    (100_000_000, "$1M-$10M"),
    (1_000_000_000, "$10M+"),
)

#: Score bands for 0-100 and bureau-style scores. A single labelling works for both because the
#: matrix only ever BANDs a normalized 0-100 propensity/behaviour figure or a FICO band, and both
#: read naturally as LOW/FAIR/GOOD/... - a FICO band is the industry's own vocabulary.
_SCORE_BANDS: Final[tuple[tuple[float, str], ...]] = (
    (0.0, "VERY_LOW"),
    (20.0, "LOW"),
    (40.0, "MEDIUM"),
    (60.0, "HIGH"),
    (80.0, "VERY_HIGH"),
)

_FICO_BANDS: Final[tuple[tuple[float, str], ...]] = (
    (300.0, "POOR"),
    (580.0, "FAIR"),
    (670.0, "GOOD"),
    (740.0, "VERY_GOOD"),
    (800.0, "EXCELLENT"),
)


def band_currency(cents: int | None) -> str | None:
    """Map an integer-cents amount to a coarse labelled band. Negative folds to its magnitude."""
    if cents is None:
        return None
    magnitude = abs(int(cents))
    label = _CURRENCY_BANDS[0][1]
    for lower, name in _CURRENCY_BANDS:
        if magnitude >= lower:
            label = name
        else:
            break
    return label


def band_score(value: float | None, *, fico: bool = False) -> str | None:
    """Map a score to a labelled band. ``fico`` selects the bureau-score vocabulary."""
    if value is None:
        return None
    table = _FICO_BANDS if fico else _SCORE_BANDS
    label = table[0][1]
    for lower, name in table:
        if value >= lower:
            label = name
        else:
            break
    return label
