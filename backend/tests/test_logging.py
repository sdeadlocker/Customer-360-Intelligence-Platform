"""Task 0.4: JSON logs, correlation IDs and non-bypassable redaction."""

from __future__ import annotations

import json
import logging

import pytest

from c360.core.context import correlation_scope, get_correlation_id, require_correlation_id
from c360.core.logging import configure_logging, get_logger
from tests.conftest import make_settings


def _emit(**kwargs: object) -> None:
    get_logger("c360.test").info("test message", extra=dict(kwargs))


class TestJsonFormat:
    def test_lines_are_json_with_the_expected_shape(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(make_settings(), force=True)
        with correlation_scope("abc123def456"):
            get_logger("c360.test").info("hello")

        record = json.loads(capsys.readouterr().out.strip())
        assert record["message"] == "hello"
        assert record["level"] == "INFO"
        assert record["logger"] == "c360.test"
        assert record["correlation_id"] == "abc123def456"
        assert record["service"] == "c360-api-test"
        assert record["environment"] == "local"
        assert "timestamp" in record
        assert "source" in record

    def test_extras_are_included(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(make_settings(), force=True)
        with correlation_scope():
            _emit(http_route="/health", duration_ms=1.5)

        record = json.loads(capsys.readouterr().out.strip())
        assert record["http_route"] == "/health"
        assert record["duration_ms"] == 1.5

    def test_console_format_is_available_for_local_use(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(make_settings(log_format="console"), force=True)
        with correlation_scope("feedfacefeedface"):
            get_logger("c360.test").warning("plain output")

        out = capsys.readouterr().out
        assert "plain output" in out
        assert "feedfacefeedface" in out

    def test_exceptions_are_rendered_and_redacted(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(make_settings(), force=True)
        with correlation_scope():
            try:
                raise ValueError("balance was $1,234.56")
            except ValueError:
                get_logger("c360.test").exception("boom")

        record = json.loads(capsys.readouterr().out.strip())
        assert "exception" in record
        assert "1,234.56" not in json.dumps(record)


class TestCorrelationId:
    def test_a_line_without_a_bound_id_still_carries_a_placeholder(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(make_settings(), force=True)
        get_logger("c360.test").info("no scope")
        assert json.loads(capsys.readouterr().out.strip())["correlation_id"] == "-"

    def test_scope_restores_the_previous_value(self) -> None:
        assert get_correlation_id() is None
        with correlation_scope("aaaabbbbccccdddd"):
            assert get_correlation_id() == "aaaabbbbccccdddd"
        assert get_correlation_id() is None

    def test_background_work_gets_an_id_rather_than_none(self) -> None:
        assert len(require_correlation_id()) == 32

    def test_trace_fields_are_present_even_outside_a_trace(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(make_settings(), force=True)
        get_logger("c360.test").info("untraced")
        record = json.loads(capsys.readouterr().out.strip())
        assert record["trace_id"] == "-"
        assert record["span_id"] == "-"


class TestRedactionIsNotBypassable:
    """The filter is on the handler, so no logger, level or call style avoids it."""

    def test_interpolated_message_arguments_are_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(make_settings(), force=True)
        get_logger("c360.test").info("customer %s owes %s", "a@b.com", "$4,500.00")

        line = capsys.readouterr().out
        assert "a@b.com" not in line
        assert "4,500.00" not in line

    def test_structured_pii_and_money_are_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(make_settings(), force=True)
        _emit(email="x@y.com", balance_cents=999_999, customer_id="CUST-0007")

        record = json.loads(capsys.readouterr().out.strip())
        assert record["email"] == "[REDACTED]"
        assert record["balance_cents"] == "[REDACTED]"
        assert record["customer_id"] == "CUST-0007"

    def test_a_third_party_logger_is_also_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(make_settings(), force=True)
        logging.getLogger("some.vendor.library").warning("token for user@bank.com refreshed")
        assert "user@bank.com" not in capsys.readouterr().out

    def test_configure_is_idempotent(self) -> None:
        settings = make_settings()
        configure_logging(settings, force=True)
        configure_logging(settings)
        assert len(logging.getLogger().handlers) == 1

    def test_uvicorn_loggers_are_routed_through_the_root_handler(self) -> None:
        configure_logging(make_settings(), force=True)
        access = logging.getLogger("uvicorn.access")
        assert access.handlers == []
        assert access.propagate is True
