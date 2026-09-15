"""Task 8.5 — the claim validator (design §8.4).

The two rejections the task names explicitly are asserted directly: a fabricated figure (no matching
fact) and a figure sourced only from a passage are both rejected. Plus the grounded-accept path, the
tolerance behaviour, the mismatch and unknown-fact cases, and the multi-number windowing that stops
one citation laundering an adjacent uncited number.
"""

from __future__ import annotations

from c360.agents.validator import validate_claims
from c360.tools.facts import FactTable


def _facts() -> FactTable:
    b = FactTable.builder()
    b.add(entity_type="customer", entity_id="C1", field="net_worth_cents", value=4_200_000)
    b.add(entity_type="customer", entity_id="C1", field="fico_score", value=780)
    b.add(entity_type="customer", entity_id="C1", field="segment", value="HNW")  # non-numeric
    return b.build()


def test_grounded_figures_pass():
    text = "Net worth is 4200000 [F1] and FICO is 780 [F2]."
    assert validate_claims(text, _facts()).ok


def test_fabricated_figure_is_rejected():
    # 9999999 matches no fact and carries no citation.
    result = validate_claims("Net worth is 9999999.", _facts())
    assert not result.ok
    assert any("no citation" in v.reason for v in result.violations)


def test_passage_sourced_figure_is_rejected():
    # A number cited to a passage is rejected even though the number is plausible.
    result = validate_claims("The rate is 5 [P1] percent.", _facts())
    assert not result.ok
    assert any("passage" in v.reason for v in result.violations)


def test_number_matching_wrong_fact_is_rejected():
    # 4200000 is a real fact value but cited to F2 (fico=780) -> mismatch.
    result = validate_claims("Net worth is 4200000 [F2].", _facts())
    assert not result.ok
    assert any("does not match F2" in v.reason for v in result.violations)


def test_unknown_fact_id_is_rejected():
    result = validate_claims("Value 4200000 [F9].", _facts())
    assert not result.ok
    assert any("unknown fact F9" in v.reason for v in result.violations)


def test_exact_match_required_by_default():
    # Off by one, zero tolerance -> rejected.
    assert not validate_claims("Net worth is 4200001 [F1].", _facts()).ok


def test_relative_tolerance_allows_rounding_when_opted_in():
    result = validate_claims("About 4200500 [F1].", _facts(), relative_tolerance=0.001)
    assert result.ok


def test_citation_does_not_launder_adjacent_uncited_number():
    # 500 has no citation of its own; F1 governs 4200000 only, not 500.
    text = "Net worth 4200000 [F1] rose by 500 last year."
    result = validate_claims(text, _facts())
    assert not result.ok
    assert any(v.number == "500" for v in result.violations)


def test_thousands_separators_are_handled():
    assert validate_claims("Net worth is 4,200,000 [F1].", _facts()).ok


def test_summary_is_value_free_but_specific():
    result = validate_claims("Net worth is 9999999.", _facts())
    assert "no citation" in result.summary()
    assert validate_claims("all good", _facts()).summary() == "ok"


def test_non_numeric_narrative_passes():
    assert validate_claims("This customer is in the HNW segment [F3].", _facts()).ok
