"""Redaction primitives shared by logging and telemetry.

Requirement 18.8 and design §13.4 are absolute: no personally identifying data, no monetary value,
no prompt or completion content may leave the process through a log line, a span or a metric. That
guarantee cannot rest on every future call site remembering it, so redaction is enforced centrally
and by *name and shape* rather than by trust.

Two layers, deliberately overlapping:

* **Structural** — a key whose name marks it as identifying or monetary is replaced wholesale.
* **Textual** — free text is scanned for values that look identifying or monetary, because the
  most common leak is an f-string in a log message, not a structured field.
"""

from __future__ import annotations

import re
from typing import Any, Final

REDACTED: Final = "[REDACTED]"

# ----------------------------------------------------------------- structural rules

#: Field names that always carry identifying data.
PII_KEYS: Final[frozenset[str]] = frozenset(
    {
        "account_number",
        "address",
        "address_line1",
        "address_line2",
        "aadhaar",
        "card_number",
        "customer_name",
        "date_of_birth",
        "dob",
        "email",
        "email_address",
        "first_name",
        "full_name",
        "given_name",
        "ifsc",
        "last_name",
        "loan_number",
        "middle_name",
        "mobile",
        "mobile_number",
        "name",
        "national_id",
        "pan",
        "pan_number",
        "passport",
        "phone",
        "phone_number",
        "postal_code",
        "registration_number",
        "ssn",
        "street",
        "street_address",
        "surname",
        "tax_id",
        "vin",
        "zip",
        "zipcode",
    }
)

#: Field names that always carry a monetary value.
MONETARY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "amount",
        "balance",
        "credit_limit",
        "emi",
        "exposure",
        "income",
        "installment",
        "limit",
        "net_worth",
        "outstanding",
        "premium",
        "price",
        "principal",
        "salary",
        "total",
        "value",
    }
)

#: Field-name fragments that mark a monetary value regardless of the rest of the name.
MONETARY_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "_amount",
    "amount_",
    "_balance",
    "balance_",
    "_cents",
    "cents_",
    "_income",
    "_salary",
    "_net_worth",
    "_exposure",
    "_outstanding",
    "_premium",
)

#: Field names that carry model prompt or completion content.
CONTENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "answer",
        "completion",
        "generation",
        "message",
        "messages",
        "narrative",
        "output_text",
        "prompt",
        "prompt_text",
        "rendered_prompt",
        "response_text",
        "system_prompt",
        "user_message",
    }
)

# ----------------------------------------------------------------- textual rules

_EMAIL: Final = re.compile(r"\b[\w.%+-]+@[\w-]+\.[A-Za-z]{2,}\b")

# Long digit runs: account numbers, card numbers, national identifiers. Grouping separators are
# included so "4111 1111 1111 1111" and "4111-1111-1111-1111" are caught as well.
_LONG_DIGITS: Final = re.compile(r"\b\d[\d\s-]{9,}\d\b")

# Currency-marked amounts, either symbol-first or code-first, with optional grouping and decimals.
_CURRENCY: Final = re.compile(
    r"(?:(?:[$€£¥₹]|\b(?:USD|EUR|GBP|INR|JPY|AUD|CAD|CHF|SGD|AED)\b)\s*"
    r"\d[\d,_]*(?:\.\d+)?)"
    r"|(?:\b\d[\d,_]*(?:\.\d+)?\s*(?:USD|EUR|GBP|INR|JPY|AUD|CAD|CHF|SGD|AED)\b)",
    re.IGNORECASE,
)

# Decimal amounts with thousands separators — "1,234.56" is a monetary value in this domain
# whether or not a currency marker survived the surrounding formatting.
_GROUPED_DECIMAL: Final = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b")

_TEXT_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (_EMAIL, "[REDACTED_EMAIL]"),
    (_CURRENCY, "[REDACTED_AMOUNT]"),
    (_GROUPED_DECIMAL, "[REDACTED_AMOUNT]"),
    (_LONG_DIGITS, "[REDACTED_NUMBER]"),
)

_MAX_TEXT_LENGTH: Final = 4_096
_MAX_DEPTH: Final = 6


def is_pii_key(key: str) -> bool:
    """Return whether a field name denotes identifying data."""
    normalised = key.strip().lower().lstrip("_")
    return normalised in PII_KEYS


def is_monetary_key(key: str) -> bool:
    """Return whether a field name denotes a monetary value."""
    normalised = key.strip().lower().lstrip("_")
    if normalised in MONETARY_KEYS:
        return True
    return any(fragment in normalised for fragment in MONETARY_KEY_FRAGMENTS)


def is_content_key(key: str) -> bool:
    """Return whether a field name denotes model prompt or completion content."""
    return key.strip().lower().lstrip("_") in CONTENT_KEYS


def is_sensitive_key(key: str) -> bool:
    """Return whether a field name must never be emitted."""
    return is_pii_key(key) or is_monetary_key(key) or is_content_key(key)


def redact_text(value: str) -> str:
    """Replace identifying and monetary patterns in free text."""
    if len(value) > _MAX_TEXT_LENGTH:
        value = value[:_MAX_TEXT_LENGTH] + "…[TRUNCATED]"
    for pattern, replacement in _TEXT_RULES:
        value = pattern.sub(replacement, value)
    return value


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a value, applying structural rules to mapping keys.

    Depth is bounded: a cyclic or pathologically nested structure in a log call must not be able
    to hang the logging thread.
    """
    if _depth >= _MAX_DEPTH:
        return REDACTED

    if isinstance(value, str):
        return redact_text(value)

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            redacted[key] = (
                REDACTED if is_sensitive_key(key) else redact_value(item, _depth=_depth + 1)
            )
        return redacted

    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item, _depth=_depth + 1) for item in value]

    if isinstance(value, (bool, int, float, type(None))):
        return value

    return redact_text(str(value))


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Redact a flat or nested mapping of structured log fields."""
    result = redact_value(mapping)
    if isinstance(result, dict):
        return result
    return {"value": result}
