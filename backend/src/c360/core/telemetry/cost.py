"""Estimated model-cost attribution (task 10.2, design §13.3 Model row).

The cost counter is *derived*, never measured: it is token counts multiplied by a configured price
table, not a bill from AWS. Design §13.3 is explicit that estimated spend is computed from token
counts against a price table (``config/bedrock_prices.json``), so this module owns the price table
and the arithmetic, and :mod:`c360.core.telemetry.metrics` owns the counter.

Prices are expressed as **integer micro-USD per 1,000 tokens** (see the price file's header). Money
is never a float on this platform, so cost accumulates in integer micro-USD and only becomes a
decimal at a reporting boundary — the same rule the customer ledger follows with cents. Micro-USD
is the working unit because a single 1k-token call costs a fraction of a cent, and rounding to
cents per call would floor most calls to zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from c360.core.logging import get_logger

_logger = get_logger(__name__)

#: Tokens are priced per thousand.
_TOKENS_PER_PRICE_UNIT: Final = 1000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Input and output price for one model, in micro-USD per 1,000 tokens."""

    input_micro_usd_per_1k: int
    output_micro_usd_per_1k: int


class CostEstimator:
    """Turns token counts into an estimated cost in integer micro-USD.

    Loaded once from the price table. A model absent from the table costs zero and is logged once —
    a missing price must never raise on the metric path, because a metric mistake must not fail a
    request, and a zero cost is a visible "priced model unknown" signal on the dashboard rather
    than a silent crash.
    """

    __slots__ = ("_prices", "_warned")

    def __init__(self, prices: dict[str, ModelPrice]) -> None:
        self._prices = prices
        self._warned: set[str] = set()

    @classmethod
    def from_file(cls, path: Path) -> CostEstimator:
        """Load the price table. A missing or malformed file yields an empty (all-zero) table."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning(
                "cost price table unavailable; estimated cost will be zero",
                extra={"path": str(path), "error_type": type(exc).__name__},
            )
            return cls({})

        models = raw.get("models", {})
        prices: dict[str, ModelPrice] = {}
        for model_id, entry in models.items():
            try:
                prices[model_id] = ModelPrice(
                    input_micro_usd_per_1k=int(entry["input"]),
                    output_micro_usd_per_1k=int(entry["output"]),
                )
            except (KeyError, TypeError, ValueError):
                _logger.warning("malformed price entry ignored", extra={"model_id": model_id})
        return cls(prices)

    def micro_usd(self, model_id: str, *, input_tokens: int, output_tokens: int) -> int:
        """Estimated cost of one call in integer micro-USD.

        Integer arithmetic throughout: ``tokens * price // 1000`` keeps the result exact to the
        micro-USD without a float ever entering the ledger.
        """
        price = self._prices.get(model_id)
        if price is None:
            if model_id not in self._warned:
                self._warned.add(model_id)
                _logger.warning(
                    "no price for model; cost recorded as zero",
                    extra={"model_id": model_id},
                )
            return 0
        input_cost = input_tokens * price.input_micro_usd_per_1k // _TOKENS_PER_PRICE_UNIT
        output_cost = output_tokens * price.output_micro_usd_per_1k // _TOKENS_PER_PRICE_UNIT
        return input_cost + output_cost


__all__ = ["CostEstimator", "ModelPrice"]
