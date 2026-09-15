"""Field-masking serializer (task 4.4).

The single choke point every customer-data response passes through. Given a domain model and a
:class:`FieldPolicy`, :func:`mask_model` produces the JSON-ready dict that goes into the envelope's
``data`` and the list of field paths that go into ``meta.masked_fields``. Design §7.2 requires this
to be *unskippable* — "so no route handler can forget it" — which is why the API layer wraps every
customer payload with :func:`mask_payload` rather than serializing models directly.

The two guarantees that matter
------------------------------

* A ``HIDDEN`` field is **absent** from the output dict, not present with a placeholder
  (requirement 12.4: "the unmasked value SHALL NOT be present in the response payload"). A caller
  parsing the JSON finds no key, and ``meta.masked_fields`` tells the UI why.
* ``PARTIAL`` and ``BAND`` replace the value with a *derived* form — last-4, a year, a band label —
  computed by :mod:`c360.security.masking`. The original never appears, so a partial account number
  is genuinely four digits, not a full number the UI is trusted to truncate.

Nested models are walked recursively, and masked paths are reported dotted
(``account.balance_cents``) so the UI can locate them precisely. The walk is driven by the model's
own field structure, so a new field is unmasked by default and a new *sensitive* field is caught by
the role x field test the moment its group is declared in the field map.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from c360.security.field_map import FieldRule, rules_for
from c360.security.masking import (
    MaskStyle,
    band_currency,
    band_score,
    partial_last4,
    partial_name,
    partial_vin,
    partial_year,
    redact,
)
from c360.security.model import MaskMode

if TYPE_CHECKING:
    from c360.security.policy import FieldPolicy


def mask_model(model: BaseModel, policy: FieldPolicy) -> tuple[dict[str, Any], list[str]]:
    """Serialize ``model`` under ``policy``, returning the masked dict and the masked field paths.

    The paths are sorted and de-duplicated so ``meta.masked_fields`` is stable across requests — a
    diff of two responses should not churn on ordering.
    """
    masked_fields: list[str] = []
    data = _mask_value(model, policy, prefix="", masked=masked_fields)
    assert isinstance(data, dict)  # noqa: S101 - a model always serializes to a dict
    return data, sorted(set(masked_fields))


def _mask_value(value: Any, policy: FieldPolicy, *, prefix: str, masked: list[str]) -> Any:
    """Recursively serialize ``value``, applying masking to any model fields it contains."""
    if isinstance(value, BaseModel):
        return _mask_model_fields(value, policy, prefix=prefix, masked=masked)
    if isinstance(value, (list, tuple)):
        return [
            _mask_value(item, policy, prefix=f"{prefix}[{index}]", masked=masked)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: _mask_value(item, policy, prefix=_join(prefix, str(key)), masked=masked)
            for key, item in value.items()
        }
    return _jsonable(value)


def _mask_model_fields(
    model: BaseModel, policy: FieldPolicy, *, prefix: str, masked: list[str]
) -> dict[str, Any]:
    rules = rules_for(type(model).__name__)
    out: dict[str, Any] = {}
    for name in type(model).model_fields:
        raw = getattr(model, name)
        path = _join(prefix, name)
        rule = rules.get(name)

        if rule is None:
            # No masking rule: recurse (the value may itself be or contain a model) and keep it.
            out[name] = _mask_value(raw, policy, prefix=path, masked=masked)
            continue

        mode = policy.mode_for(rule.group)
        if mode is MaskMode.FULL:
            out[name] = _jsonable(raw)
        elif mode is MaskMode.HIDDEN:
            # Dropped entirely — the key does not appear. Recorded so the UI can explain the gap.
            masked.append(path)
            _record_masking(rule.group.name)
        else:  # PARTIAL or BAND
            out[name] = _apply(mode, rule, raw)
            masked.append(path)
            _record_masking(rule.group.name)
    return out


def _record_masking(field_group: str) -> None:
    """Increment the masking-applied counter, labelled by field group (task 10.2, design §13.3).

    A masking application is a security event worth a dashboard line — how much of which group is
    being withheld, by aggregate, never per customer. The field group is low-cardinality and
    non-identifying; no field value is ever a label.
    """
    from c360.core.telemetry import get_metrics  # noqa: PLC0415 - avoid import cycle at load

    metrics = get_metrics()
    if metrics is not None:
        metrics.record_masking(field_group=field_group)


def _apply(mode: MaskMode, rule: FieldRule, value: Any) -> Any:
    """Compute the PARTIAL or BAND form of ``value`` for its style."""
    if mode is MaskMode.BAND:
        return _apply_band(rule, value)
    return _apply_partial(rule, value)


def _apply_band(rule: FieldRule, value: Any) -> Any:
    if rule.style is MaskStyle.CURRENCY:
        return band_currency(value)
    if rule.style is MaskStyle.SCORE:
        return band_score(_as_float(value), fico=rule.group.name == "CREDIT_SCORE")
    # A BAND on a non-numeric field degrades to redaction rather than leaking the value.
    return redact(value)


#: PARTIAL transforms that operate on the value's string form, by style.
_PARTIAL_TEXT_TRANSFORMS = {
    MaskStyle.LAST4: partial_last4,
    MaskStyle.YEAR_ONLY: partial_year,
    MaskStyle.NAME: partial_name,
    MaskStyle.VIN: partial_vin,
}


def _apply_partial(rule: FieldRule, value: Any) -> Any:
    transform = _PARTIAL_TEXT_TRANSFORMS.get(rule.style)
    if transform is not None:
        return transform(None if value is None else str(value))
    if rule.style is MaskStyle.CURRENCY:
        # A "partial" currency is meaningless; band it instead of revealing digits.
        return band_currency(value)
    if rule.style is MaskStyle.SCORE:
        return band_score(_as_float(value), fico=rule.group.name == "CREDIT_SCORE")
    return redact(value)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - defends a mis-declared style
        return None


def _jsonable(value: Any) -> Any:
    """Coerce a leaf value into something JSON-serializable, matching the models' serializers."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _join(prefix: str, name: str) -> str:
    return name if not prefix else f"{prefix}.{name}"
