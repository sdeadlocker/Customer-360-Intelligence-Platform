"""Task 8.4 — the PromptRedactor (design §8.6).

Asserts data minimisation before dispatch: only allowlisted fields survive, the never-dispatch floor
removes PAN/account/VIN/DOB/street even if allowlisted, identifiers are pseudonymised consistently
and re-hydrated on output, the field manifest reflects what was sent, and retrieval queries are
scrubbed.
"""

from __future__ import annotations

from c360.agents.redaction import NEVER_DISPATCH_FIELDS, PromptRedactor
from c360.tools.facts import FactTable


def _facts() -> FactTable:
    b = FactTable.builder()
    b.add(entity_type="customer", entity_id="CUST-9001", field="net_worth_cents", value=4_200_000)
    b.add(entity_type="customer", entity_id="CUST-9001", field="customer_segment", value="HNW")
    b.add(entity_type="customer", entity_id="CUST-9001", field="date_of_birth", value="1980-01-01")
    b.add(entity_type="account", entity_id="ACC-55", field="account_number", value="12345678")
    b.add(entity_type="account", entity_id="ACC-55", field="balance_cents", value=90_000)
    return b.build()


_ALLOWLIST = frozenset(
    {"net_worth_cents", "customer_segment", "date_of_birth", "account_number", "balance_cents"}
)


def test_only_allowlisted_fields_survive():
    ctx = PromptRedactor().redact(_facts(), allowlist=frozenset({"net_worth_cents"}))
    kept = {f.field for f in ctx.facts.facts}
    assert kept == {"net_worth_cents"}


def test_never_dispatch_floor_removes_pii_even_when_allowlisted():
    # date_of_birth and account_number ARE on the allowlist, yet the floor strips them.
    ctx = PromptRedactor().redact(_facts(), allowlist=_ALLOWLIST)
    kept = {f.field for f in ctx.facts.facts}
    assert "date_of_birth" not in kept
    assert "account_number" not in kept
    assert kept == {"net_worth_cents", "customer_segment", "balance_cents"}


def test_never_dispatch_covers_the_documented_fields():
    for field in (
        "pan",
        "card_number",
        "account_number",
        "vin",
        "date_of_birth",
        "address_line1",
        "address_line2",
        "ssn",
    ):
        assert field in NEVER_DISPATCH_FIELDS


def test_field_manifest_reflects_surviving_fields():
    ctx = PromptRedactor().redact(_facts(), allowlist=_ALLOWLIST)
    assert ctx.field_manifest == ("balance_cents", "customer_segment", "net_worth_cents")


def test_customer_and_account_ids_are_pseudonymised():
    ctx = PromptRedactor().redact(_facts(), allowlist=_ALLOWLIST)
    ids = {f.entity_id for f in ctx.facts.facts}
    assert "CUST-9001" not in ids
    assert "ACC-55" not in ids
    assert "CUST_A" in ids
    assert "ACCT_1" in ids


def test_pseudonyms_are_stable_across_agents_in_one_run():
    redactor = PromptRedactor()
    first = redactor.redact(_facts(), allowlist=_ALLOWLIST)
    second = redactor.redact(_facts(), allowlist=_ALLOWLIST)
    # Same real id maps to the same pseudonym across both redactions.
    assert {f.entity_id for f in first.facts.facts} == {f.entity_id for f in second.facts.facts}


def test_rehydrate_restores_real_identifiers():
    redactor = PromptRedactor()
    redactor.redact(_facts(), allowlist=_ALLOWLIST)
    narrative = "CUST_A holds account ACCT_1 with a healthy balance."
    assert redactor.rehydrate(narrative) == "CUST-9001 holds account ACC-55 with a healthy balance."


def test_context_rehydrate_matches_redactor():
    redactor = PromptRedactor()
    ctx = redactor.redact(_facts(), allowlist=_ALLOWLIST)
    assert ctx.rehydrate("CUST_A") == "CUST-9001"


def test_customer_letters_roll_over_past_26():
    redactor = PromptRedactor()
    b = FactTable.builder()
    for i in range(28):
        b.add(entity_type="customer", entity_id=f"C{i}", field="customer_segment", value="X")
    ctx = redactor.redact(b.build(), allowlist=frozenset({"customer_segment"}))
    ids = [f.entity_id for f in ctx.facts.facts]
    assert "CUST_A" in ids
    assert "CUST_Z" in ids
    assert "CUST_AA" in ids


def test_redact_query_scrubs_pii():
    redactor = PromptRedactor()
    scrubbed = redactor.redact_query("eligibility for account 123456789 owner jane@example.com")
    assert "123456789" not in scrubbed
    assert "jane@example.com" not in scrubbed
    assert "eligibility" in scrubbed
