"""Domain value types, models and repository ports.

This package holds what the application knows about banking, independent of how any of it is
stored. Design §2.2 makes that separation a rule rather than a preference: services depend on the
ports declared here, and :mod:`c360.data` supplies the SQLite implementations of them.

Nothing in here imports from :mod:`c360.data`, and nothing in here imports SQLAlchemy. That is the
property that makes the PostgreSQL swap path in design §1 a new adapter rather than a rewrite, and
it is checked by :func:`tests.test_domain_ports.test_domain_package_does_not_depend_on_storage`.
"""

from __future__ import annotations

from c360.domain.dates import (
    ISO_DATE_LENGTH,
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

__all__ = [
    "BPS_PER_UNIT",
    "CENTS_PER_UNIT",
    "ISO_DATE_LENGTH",
    "ZERO_BPS",
    "ZERO_CENTS",
    "Bps",
    "Cents",
    "InvalidIsoDateError",
    "MoneyConversionError",
    "Rounding",
    "from_iso",
    "is_iso_date",
    "optional_from_iso",
    "optional_to_iso",
    "to_iso",
    "to_iso_utc",
    "today_iso",
]
