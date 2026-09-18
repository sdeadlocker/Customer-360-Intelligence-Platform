"""The deposit fee schedule (Phase 22 play 6).

Fee recovery asks one question per billing cycle: *was the bank entitled to a fee here, and did it
take it?* Answering that needs the pricing rules in a form code can evaluate, which is what this
module provides — loaded from ``config/fee_schedule.json``, whose every amount is transcribed from a
product sheet's "Rates and Fees" table in the knowledge corpus.

Two design rules matter here.

**The corpus stays authoritative.** Each rule carries the ``doc_id`` and a human ``rule_basis`` of
the document it came from, so a finding cites the pricing document rather than asserting a number on
its own authority. That is the same discipline the claim validator applies to agent output: a figure
must resolve to a source.

**A malformed schedule raises.** :class:`c360.core.telemetry.cost.CostEstimator` deliberately
degrades to an all-zero table because a metric must never fail a request. The opposite is true here.
An empty fee schedule would make the detector find no leakage, and "no leakage" reads as good news —
a silent false negative is worse than a loud failure, so :meth:`FeeSchedule.from_file` raises
:class:`FeeScheduleError` and the caller decides how to surface it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from c360.domain.money import Cents

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "FEE_CATEGORY",
    "FEE_MERCHANT_MAINTENANCE",
    "FEE_MERCHANT_WIRE",
    "WIRE_EVENT_MERCHANT",
    "FeeSchedule",
    "FeeScheduleError",
    "ProductFeeRule",
    "WaiverRule",
    "default_schedule_path",
]

# ---------------------------------------------------------------- the shared fee vocabulary
#
# The generator writes these strings and the detector matches on them, so they live here rather than
# being duplicated on either side. `transaction_category` is the only column that separates a fee
# from ordinary spend, and `merchant` is the only column that separates one kind of fee from another
# — the transaction table has no fee-type column — so these four literals are load-bearing. Changing
# one without the other would make the detector silently find nothing, which is why they are shared
# constants and asserted in the generator's tests.

#: `txn.transaction_category` for every fee posting and every fee reversal.
FEE_CATEGORY: Final = "FEES"

#: `txn.merchant` for a cycle's maintenance fee (debit) and its reversal (credit).
FEE_MERCHANT_MAINTENANCE: Final = "Monthly Maintenance Fee"

#: `txn.merchant` for a per-wire service fee.
FEE_MERCHANT_WIRE: Final = "Wire Transfer Fee"

#: `txn.merchant` of the billable *event* a wire fee should accompany. Not itself a fee, so it is
#: categorized as a transfer; the unbilled-service detector pairs these against wire-fee postings.
WIRE_EVENT_MERCHANT: Final = "Outgoing Domestic Wire"


def default_schedule_path() -> Path:
    """The shipped schedule's location, resolved from this package rather than from Settings.

    The data generator has no configuration dependency by design — it is importable and runnable
    without a ``Settings`` instance — so it needs a way to find the schedule that does not route
    through :class:`c360.core.config.Settings`. The application still resolves the path from
    ``FEE_SCHEDULE_PATH``; this is the fallback both share by pointing at the same committed file.
    """
    return Path(__file__).resolve().parents[4] / "config" / "fee_schedule.json"


class FeeScheduleError(Exception):
    """The fee schedule is missing, unreadable, or does not describe a usable rule set."""


@dataclass(frozen=True, slots=True)
class WaiverRule:
    """The conditions that waive a cycle's maintenance fee. Any one of them is sufficient.

    A rule with no conditions at all never waives, which is the correct reading of a product sheet
    that lists a maintenance fee and no waiver against it.
    """

    min_balance_cents: Cents | None = None
    min_monthly_credit_cents: Cents | None = None
    min_relationship_balance_cents: Cents | None = None

    @property
    def is_unconditional_charge(self) -> bool:
        """True when nothing waives the fee, so every cycle is billable."""
        return (
            self.min_balance_cents is None
            and self.min_monthly_credit_cents is None
            and self.min_relationship_balance_cents is None
        )

    def waives(
        self,
        *,
        balance_cents: Cents,
        monthly_credit_cents: Cents,
        relationship_balance_cents: Cents,
    ) -> bool:
        """Whether this cycle qualified for a waiver.

        Every comparison is integer cents. The caller supplies zero rather than ``None`` for a
        quantity it could not measure, which fails toward "not waived" — the conservative direction
        for a *detector*, because a wrongly-waived cycle produces a missed finding rather than a
        false accusation. See :meth:`explain` for the text a finding shows.
        """
        if self.min_balance_cents is not None and balance_cents >= self.min_balance_cents:
            return True
        if (
            self.min_monthly_credit_cents is not None
            and monthly_credit_cents >= self.min_monthly_credit_cents
        ):
            return True
        return (
            self.min_relationship_balance_cents is not None
            and relationship_balance_cents >= self.min_relationship_balance_cents
        )

    def explain(self) -> str:
        """A short human phrase naming the waiver conditions, for a finding's evidence line."""
        parts: list[str] = []
        if self.min_balance_cents is not None:
            parts.append(f"balance of {_dollars(self.min_balance_cents)}")
        if self.min_monthly_credit_cents is not None:
            parts.append(f"recurring credit of {_dollars(self.min_monthly_credit_cents)}")
        if self.min_relationship_balance_cents is not None:
            parts.append(f"relationship balance of {_dollars(self.min_relationship_balance_cents)}")
        if not parts:
            return "no waiver applies to this product"
        return "waived by " + " or ".join(parts)


@dataclass(frozen=True, slots=True)
class ProductFeeRule:
    """The billable items for one product code, and the document they are transcribed from."""

    product_code: str
    doc_id: str
    rule_basis: str
    maintenance_fee_cents: Cents
    waiver: WaiverRule
    #: Per-event fee for an outgoing domestic wire, where the product sheet prices one. ``None``
    #: means the sheet either includes wires at no charge or does not offer them, so an outgoing
    # wire : on this product is not billable and raises no finding.
    outgoing_wire_fee_cents: Cents | None = None


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Every product's billable items, keyed by product code."""

    version: str
    products: Mapping[str, ProductFeeRule]
    #: The policy document behind a waiver-pattern finding. The corpus requires supervisor review of
    #: repeated waivers but states no numeric allowance, so the *count* is a configured threshold
    # and : only the review requirement is cited. See ``config/fee_schedule.json``.
    waiver_policy_doc_id: str
    waiver_policy_rule_basis: str

    def rule_for(self, product_code: str | None) -> ProductFeeRule | None:
        """The rule for a product code, or ``None`` when nothing about it is billable."""
        if product_code is None:
            return None
        return self.products.get(product_code)

    @classmethod
    def from_file(cls, path: Path) -> FeeSchedule:
        """Load and validate the schedule.

        Raises:
            FeeScheduleError: the file is absent, unparseable, or describes no products.
        """
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FeeScheduleError(f"fee schedule unreadable at {path}") from exc
        except json.JSONDecodeError as exc:
            raise FeeScheduleError(f"fee schedule is not valid JSON at {path}") from exc
        return cls.from_mapping(raw, source=str(path))

    @classmethod
    def from_mapping(cls, raw: Any, *, source: str = "<mapping>") -> FeeSchedule:
        """Build a schedule from an already-parsed mapping, validating every field."""
        if not isinstance(raw, dict):
            raise FeeScheduleError(f"fee schedule must be an object at {source}")

        entries = raw.get("products")
        if not isinstance(entries, dict) or not entries:
            raise FeeScheduleError(f"fee schedule declares no products at {source}")

        products: dict[str, ProductFeeRule] = {}
        for product_code, entry in entries.items():
            products[str(product_code)] = _rule_from_entry(str(product_code), entry, source=source)

        policy = raw.get("waiver_policy")
        policy_doc = _optional_str(policy, "doc_id") if isinstance(policy, dict) else None
        policy_basis = _optional_str(policy, "rule_basis") if isinstance(policy, dict) else None
        return cls(
            version=str(raw.get("version", "unversioned")),
            products=products,
            waiver_policy_doc_id=policy_doc or "",
            waiver_policy_rule_basis=policy_basis or "fee waiver authority policy",
        )


def _rule_from_entry(product_code: str, entry: Any, *, source: str) -> ProductFeeRule:
    if not isinstance(entry, dict):
        raise FeeScheduleError(f"fee rule for {product_code} must be an object at {source}")
    try:
        maintenance = Cents(int(entry["maintenance_fee_cents"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise FeeScheduleError(
            f"fee rule for {product_code} needs an integer maintenance_fee_cents at {source}"
        ) from exc
    if maintenance < 0:
        raise FeeScheduleError(f"maintenance_fee_cents for {product_code} cannot be negative")

    doc_id = _optional_str(entry, "doc_id")
    if doc_id is None:
        raise FeeScheduleError(
            f"fee rule for {product_code} must cite a doc_id — a finding has to name its source"
        )

    wire = entry.get("outgoing_wire_fee_cents")
    return ProductFeeRule(
        product_code=product_code,
        doc_id=doc_id,
        rule_basis=_optional_str(entry, "rule_basis") or doc_id,
        maintenance_fee_cents=maintenance,
        waiver=_waiver_from_entry(product_code, entry.get("waiver")),
        outgoing_wire_fee_cents=None if wire is None else _positive_cents(product_code, wire),
    )


def _waiver_from_entry(product_code: str, raw: Any) -> WaiverRule:
    if raw is None:
        return WaiverRule()
    if not isinstance(raw, dict):
        raise FeeScheduleError(f"waiver for {product_code} must be an object")
    return WaiverRule(
        min_balance_cents=_optional_cents(product_code, raw.get("min_balance_cents")),
        min_monthly_credit_cents=_optional_cents(product_code, raw.get("min_monthly_credit_cents")),
        min_relationship_balance_cents=_optional_cents(
            product_code, raw.get("min_relationship_balance_cents")
        ),
    )


def _optional_cents(product_code: str, value: Any) -> Cents | None:
    return None if value is None else _positive_cents(product_code, value)


def _positive_cents(product_code: str, value: Any) -> Cents:
    try:
        amount = Cents(int(value))
    except (TypeError, ValueError) as exc:
        raise FeeScheduleError(f"non-integer cents value for {product_code}") from exc
    if amount < 0:
        raise FeeScheduleError(f"negative cents value for {product_code}")
    return amount


def _optional_str(entry: Any, key: str) -> str | None:
    if not isinstance(entry, dict):
        return None
    value = entry.get(key)
    return str(value) if isinstance(value, str) and value else None


#: Cents per dollar, for the human phrasing in :meth:`WaiverRule.explain`.
_CENTS_PER_DOLLAR: Final = 100


def _dollars(amount: Cents) -> str:
    """Whole-dollar rendering for evidence text. Presentation only; the ledger stays in cents."""
    return f"${int(amount) // _CENTS_PER_DOLLAR:,}"
