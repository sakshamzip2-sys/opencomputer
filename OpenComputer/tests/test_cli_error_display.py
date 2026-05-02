"""Tests for opencomputer/cli_error_display.py — ported from Hermes pattern."""
from __future__ import annotations

from opencomputer.cli_error_display import (
    extract_friendly_message,
    format_error_for_console,
    format_provider_error_for_console,
)


class _FakeBadRequestError(Exception):
    """Mimics anthropic.BadRequestError shape: has .body + status_code."""
    def __init__(self, status_code: int, body: dict, raw: str) -> None:
        super().__init__(raw)
        self.body = body
        self.status_code = status_code


def test_extracts_anthropic_structured_message():
    err = _FakeBadRequestError(
        status_code=400,
        body={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "tools.0.custom: For 'integer' type, properties maximum, minimum are not supported",
            },
        },
        raw="Error code: 400 - {raw dict repr}",
    )
    msg = extract_friendly_message(err)
    assert msg.startswith("tools.0.custom")
    assert "minimum" in msg
    # Crucially: must NOT contain the noisy raw dict repr
    assert "{" not in msg


def test_extracts_flat_body_message_when_no_nested_error():
    err = _FakeBadRequestError(
        status_code=500,
        body={"message": "Server timed out"},
        raw="Error code: 500 - foo",
    )
    msg = extract_friendly_message(err)
    assert msg == "Server timed out"


def test_falls_back_to_str_when_no_body():
    err = ValueError("bare exception, no body attr")
    msg = extract_friendly_message(err)
    assert msg == "bare exception, no body attr"


def test_strips_error_code_preamble():
    err = ValueError("Error code: 400 - actual message")
    msg = extract_friendly_message(err)
    assert msg == "actual message"


def test_truncates_long_messages():
    err = ValueError("x" * 1000)
    msg = extract_friendly_message(err)
    assert len(msg) <= 500


def test_format_error_for_console_uses_red_markup():
    err = ValueError("boom")
    out = format_error_for_console(err)
    assert "[bold red]" in out
    assert "ValueError" in out
    assert "boom" in out
    assert "✗" in out


def test_format_provider_error_includes_http_status():
    err = _FakeBadRequestError(
        status_code=400,
        body={"error": {"message": "bad input"}},
        raw="Error code: 400 - foo",
    )
    out = format_provider_error_for_console(err)
    assert "HTTP 400" in out
    assert "bad input" in out


def test_format_provider_error_extracts_status_from_str_when_no_attr():
    """Some SDKs don't set status_code; we parse it from the str."""
    err = ValueError("Error code: 429 - rate limited")
    out = format_provider_error_for_console(err)
    assert "HTTP 429" in out


def test_format_provider_error_no_status_when_unknown():
    err = ValueError("plain exception")
    out = format_provider_error_for_console(err)
    assert "HTTP" not in out


def test_extract_handles_non_dict_body():
    """Defensive: if body isn't a dict, fall back gracefully."""
    err = ValueError("plain msg")
    err.body = "not a dict"  # type: ignore[attr-defined]
    msg = extract_friendly_message(err)
    assert msg == "plain msg"
