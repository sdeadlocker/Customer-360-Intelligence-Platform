"""The claim validator — the gate between "AI summary" and one a bank can show (task 8.5, §8.4).

A narrative is accepted only if every figure in it is backed by a fact. Concretely, for each numeric
token in the text the validator finds the citation that governs it (the ``[F..]`` / ``[P..]`` marker
that follows it, before the next number) and applies three rules:

1. A number with **no citation** is a fabrication — rejected.
2. A number cited to a **passage** (``[P..]``) is rejected: retrieval supplies rules, procedures and
   language, never values (design §9.1). This is the rule that stops a model quoting a rate table's
   figure as if it were this customer's number.
3. A number cited to a **fact** (``[F..]``) is accepted only if it matches that fact's value within
   tolerance. A cited fact that does not exist, or whose value differs, is a rejection.

On any violation the caller retries generation once with the violation named, and if the retry still
fails it renders the deterministic template (task 8.1) and marks the result ``degraded`` (§8.4). The
validator itself is pure — it decides pass/fail and lists violations; the retry/fallback control
lives in the agent runner (task 8.7).

Tolerance
---------

Figures are integer cents end to end (design §1.1), so an exact match is the norm and the default
tolerance is zero. A small relative tolerance is configurable for the rare narrative that fairly
rounds (e.g. "about 4.2 million" against ``4_200_000``) — but rounding is opt-in, because silently
accepting an approximation is exactly the failure this gate exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from c360.tools.facts import FactTable

#: A numeric token: an integer or decimal, optionally signed, with optional thousands separators.
#: Anchored so an id like ``F1`` inside a citation is never mistaken for the number ``1``.
_NUMBER_RE: Final = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w])")

#: ISO-8601 date and datetime shapes. These are temporal *context*, not monetary or count claims —
#: a narrative saying "as of 2025-09-01" is not asserting a figure a fact must back — so date-shaped
#: spans are masked out before the numeric scan runs. A model that fabricates a *date* is a
#: correctness concern the fact-coverage scorers (Phase 11) address; conflating it with a fabricated
#: dollar amount here would reject every legitimate mention of a date.
_ISO_DATE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")

#: A citation marker: ``[F12]`` or ``[P3]``. Captures the kind letter and the id.
_CITATION_RE: Final = re.compile(r"\[([FP])(\d+)\]")


@dataclass(frozen=True, slots=True)
class Violation:
    """One rejected figure: the number as written and why it was rejected."""

    number: str
    reason: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The verdict for one narrative. ``ok`` is true only when there are no violations."""

    ok: bool
    violations: tuple[Violation, ...]

    def summary(self) -> str:
        """A one-line, value-free description of the violations, for the retry instruction and span.

        Names the offending numbers and reasons so the retry prompt can be specific, but carries no
        customer identity — it is the numbers and the rule they broke, which is what the model needs
        to correct itself.
        """
        if self.ok:
            return "ok"
        return "; ".join(f"{v.number}: {v.reason}" for v in self.violations)


def validate_claims(
    text: str,
    facts: FactTable,
    *,
    relative_tolerance: float = 0.0,
) -> ValidationResult:
    """Validate every numeric claim in ``text`` against ``facts`` (task 8.5, design §8.4)."""
    fact_values = _numeric_fact_values(facts)
    fact_ids = {fact.fact_id for fact in facts.facts}
    violations: list[Violation] = []

    # Blank out ISO dates (replacing with spaces preserves every other span's offset) so the numeric
    # scan below never sees a date's component digits as figures.
    scanned = _ISO_DATE_RE.sub(lambda m: " " * len(m.group()), text)

    for match in _NUMBER_RE.finditer(scanned):
        raw = match.group()
        value = _to_number(raw)
        if value is None:
            continue  # not a real quantity (shouldn't happen given the regex), skip defensively
        governing = _governing_citation(scanned, match.end())
        if governing is None:
            violations.append(Violation(raw, "figure has no citation"))
            continue
        kind, cited_id = governing
        if kind == "P":
            violations.append(Violation(raw, f"figure cited to passage P{cited_id}, not a fact"))
            continue
        fact_id = f"F{cited_id}"
        if fact_id not in fact_ids:
            violations.append(Violation(raw, f"cites unknown fact {fact_id}"))
            continue
        fact_value = fact_values.get(fact_id)
        if fact_value is None or not _within(value, fact_value, relative_tolerance):
            violations.append(Violation(raw, f"does not match {fact_id}"))

    return ValidationResult(ok=not violations, violations=tuple(violations))


# ---------------------------------------------------------------- helpers


def _numeric_fact_values(facts: FactTable) -> dict[str, float]:
    """Map fact id -> numeric value for every fact whose value is a number.

    A string-valued fact (a segment, a band) has no numeric value and simply is not in the map, so a
    number in the narrative can never be "validated" against a non-numeric fact.
    """
    values: dict[str, float] = {}
    for fact in facts.facts:
        if isinstance(fact.value, bool):
            continue  # bool is an int subclass; a boolean fact is not a quantity
        if isinstance(fact.value, (int, float)):
            values[fact.fact_id] = float(fact.value)
    return values


def _governing_citation(text: str, start: int) -> tuple[str, str] | None:
    """The citation governing the number ending at ``start``: first marker before the next number.

    Scans forward from the number to the earliest of (the next number, end of text) and returns the
    first ``[F..]``/``[P..]`` in that window. This is what binds ``4200000 [F1]`` while refusing to
    let ``[F1]`` reach across an intervening ``9500 [F2]`` to launder an uncited figure.
    """
    next_number = _NUMBER_RE.search(text, start)
    window_end = next_number.start() if next_number else len(text)
    citation = _CITATION_RE.search(text, start, window_end)
    if citation is None:
        return None
    return citation.group(1), citation.group(2)


def _to_number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _within(claim: float, fact: float, relative_tolerance: float) -> bool:
    """Whether ``claim`` matches ``fact`` within ``relative_tolerance`` (0 means exact)."""
    if claim == fact:
        return True
    if relative_tolerance <= 0.0:
        return False
    scale = max(abs(fact), 1.0)
    return abs(claim - fact) <= relative_tolerance * scale


__all__ = ["ValidationResult", "Violation", "validate_claims"]
