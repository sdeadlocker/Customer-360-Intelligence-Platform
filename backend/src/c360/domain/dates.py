"""ISO-8601 date conversion (task 1.5).

SQLite has no ``DATE`` type, so design §1.1 stores dates as ``TEXT`` in ISO-8601 with ``CHECK``
constraints, and the migrations express that check as ``column = date(column)``.

This module is the Python side of exactly that constraint, and the tightness is the point.
:func:`datetime.date.fromisoformat` is *looser* than the database: since Python 3.11 it also accepts
the basic format ``20240115`` and ISO week dates like ``2024-W03-1``, neither of which survives
``date(x) = x`` in SQLite. A parser that accepts more than the database does means a value can pass
validation on the way in and then be rejected — or worse, silently mismatch on a string comparison —
on the way to storage. So the format is pinned to extended ``YYYY-MM-DD`` and nothing else.

Dates are compared as strings in several places (``BETWEEN`` on ``transaction_date``, ordering on
``event_date``), which is only correct for zero-padded fixed-width values. That is a second reason
``2024-1-5`` has to be a hard error rather than something to normalize quietly: a mix of widths in
one
column makes every range query subtly wrong.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Final

#: Length of an extended-format ISO-8601 calendar date.
ISO_DATE_LENGTH: Final = 10

#: Extended format only, zero-padded, no time component, no week dates, no ordinal dates.
_ISO_DATE_PATTERN: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidIsoDateError(ValueError):
    """Raised when text is not an extended-format ISO-8601 calendar date.

    Subclasses ``ValueError`` so Pydantic treats it as a validation failure, which is what makes
    requirement 14.5 — reject or quarantine records that fail schema validation — work at the model
    layer as well as at the ``CHECK`` constraint.
    """


def to_iso(value: date) -> str:
    """Render a date as ``YYYY-MM-DD``.

    A ``datetime`` is rejected rather than truncated. Truncating discards a time zone along with the
    time, and the resulting date depends on which zone the caller happened to be thinking in.
    """
    if isinstance(value, datetime):
        raise InvalidIsoDateError(
            "to_iso takes a date, not a datetime: truncating a datetime silently picks a "
            "time zone. Call .date() explicitly, or use to_iso_utc()."
        )
    if not isinstance(value, date):
        raise InvalidIsoDateError(f"to_iso requires a date, got {type(value).__name__}")
    return value.isoformat()


def to_iso_utc(value: datetime) -> str:
    """Render the UTC calendar date of a ``datetime``, making the zone choice explicit.

    A naive datetime is treated as UTC; an aware one is converted first.
    """
    if not isinstance(value, datetime):
        raise InvalidIsoDateError(f"to_iso_utc requires a datetime, got {type(value).__name__}")
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC).date().isoformat()


def from_iso(text: str) -> date:
    """Parse ``YYYY-MM-DD`` into a :class:`datetime.date`.

    Raises :class:`InvalidIsoDateError` for anything else, including forms
    :func:`date.fromisoformat` would otherwise accept.
    """
    if not isinstance(text, str):
        raise InvalidIsoDateError(f"from_iso requires a str, got {type(text).__name__}")
    if not _ISO_DATE_PATTERN.match(text):
        raise InvalidIsoDateError(
            f"{text!r} is not an ISO-8601 date in YYYY-MM-DD form. "
            "Zero-padded extended format only: no times, week dates or basic format."
        )
    try:
        # Format is already proven, so this call is only validating that the date exists — it
        # rejects 2024-02-30 and 2023-02-29.
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidIsoDateError(f"{text!r} is not a real calendar date: {exc}") from exc


def optional_to_iso(value: date | None) -> str | None:
    """:func:`to_iso`, passing ``None`` through.

    Nullable date columns are common in the schema (``close_date``, ``maturity_date``,
    ``decision_date``), and requirement 4.7 distinguishes "no value" from a zero or blank, so
    ``None`` has to survive the round trip rather than becoming the empty string.
    """
    return None if value is None else to_iso(value)


def optional_from_iso(text: str | None) -> date | None:
    """:func:`from_iso`, passing ``None`` through."""
    return None if text is None else from_iso(text)


def today_iso() -> str:
    """Today's UTC date as ``YYYY-MM-DD``.

    UTC rather than local time so a seed run and a test assertion agree regardless of the machine's
    zone — the reproducibility requirement in 14.7 in miniature.
    """
    return datetime.now(UTC).date().isoformat()


def is_iso_date(text: object) -> bool:
    """Whether ``text`` is a valid extended-format ISO-8601 date. Never raises."""
    if not isinstance(text, str) or not _ISO_DATE_PATTERN.match(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True
