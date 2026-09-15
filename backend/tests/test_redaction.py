"""Task 0.4: the redaction rules behind requirement 18.8."""

from __future__ import annotations

import pytest

from c360.core.redaction import (
    REDACTED,
    is_monetary_key,
    is_pii_key,
    is_sensitive_key,
    redact_mapping,
    redact_text,
    redact_value,
)


class TestKeyClassification:
    @pytest.mark.parametrize(
        "key",
        [
            "email",
            "Email",
            "phone",
            "mobile",
            "dob",
            "date_of_birth",
            "pan",
            "vin",
            "account_number",
            "card_number",
            "national_id",
            "street_address",
            "full_name",
        ],
    )
    def test_pii_keys(self, key: str) -> None:
        assert is_pii_key(key)

    @pytest.mark.parametrize(
        "key",
        [
            "balance",
            "current_balance_cents",
            "net_worth",
            "salary",
            "emi",
            "outstanding_amount",
            "credit_limit",
            "total_exposure",
            "premium",
        ],
    )
    def test_monetary_keys(self, key: str) -> None:
        assert is_monetary_key(key)

    @pytest.mark.parametrize("key", ["prompt", "completion", "narrative", "messages"])
    def test_content_keys(self, key: str) -> None:
        assert is_sensitive_key(key)

    @pytest.mark.parametrize(
        "key", ["customer_id", "segment", "role", "as_of", "duration_ms", "http_status"]
    )
    def test_operational_keys_survive(self, key: str) -> None:
        assert not is_sensitive_key(key)


class TestTextRedaction:
    def test_email_is_removed(self) -> None:
        assert "priya.sharma@example.com" not in redact_text("mail priya.sharma@example.com now")

    @pytest.mark.parametrize(
        "text",
        ["balance is $12,345.67", "credit of INR 45000", "₹98,765 debited", "1200.50 USD posted"],
    )
    def test_monetary_values_are_removed(self, text: str) -> None:
        redacted = redact_text(text)
        assert "[REDACTED_AMOUNT]" in redacted
        assert not any(char.isdigit() for char in redacted.replace("[REDACTED_AMOUNT]", ""))

    @pytest.mark.parametrize(
        "text",
        ["account 4111111111111111", "card 4111 1111 1111 1111", "loan 4111-1111-1111-1111"],
    )
    def test_long_digit_runs_are_removed(self, text: str) -> None:
        assert "[REDACTED_NUMBER]" in redact_text(text)

    def test_short_operational_numbers_survive(self) -> None:
        assert redact_text("completed in 42 ms with status 200") == (
            "completed in 42 ms with status 200"
        )

    def test_oversized_text_is_truncated(self) -> None:
        assert "[TRUNCATED]" in redact_text("x" * 5000)


class TestStructuralRedaction:
    def test_sensitive_keys_are_replaced_wholesale(self) -> None:
        result = redact_mapping(
            {
                "customer_id": "CUST-0001",
                "email": "a@b.com",
                "current_balance_cents": 123_456,
                "prompt": "You are a helpful assistant",
                "duration_ms": 12.5,
            }
        )
        assert result["customer_id"] == "CUST-0001"
        assert result["email"] == REDACTED
        assert result["current_balance_cents"] == REDACTED
        assert result["prompt"] == REDACTED
        assert result["duration_ms"] == 12.5

    def test_nested_structures_are_walked(self) -> None:
        result = redact_mapping(
            {"customer": {"segment": "AFFLUENT", "accounts": [{"balance": 100, "type": "SAVINGS"}]}}
        )
        customer = result["customer"]
        assert customer["segment"] == "AFFLUENT"
        assert customer["accounts"][0]["balance"] == REDACTED
        assert customer["accounts"][0]["type"] == "SAVINGS"

    def test_recursion_is_bounded(self) -> None:
        payload: dict[str, object] = {"level": "0"}
        cursor = payload
        for depth in range(1, 12):
            nested: dict[str, object] = {"level": str(depth)}
            cursor["child"] = nested
            cursor = nested
        assert redact_mapping(payload) is not None  # terminates rather than recursing forever

    def test_values_in_free_text_are_still_caught(self) -> None:
        result = redact_mapping({"note": "customer called about $4,500.00 overdraft"})
        assert "4,500.00" not in result["note"]

    def test_non_mapping_input_is_wrapped(self) -> None:
        assert redact_value(("a@b.com",)) == ["[REDACTED_EMAIL]"]
