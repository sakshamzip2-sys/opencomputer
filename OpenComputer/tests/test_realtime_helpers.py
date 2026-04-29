"""Pure helpers ported from openclaw/extensions/openai/realtime-provider-shared.ts."""
from __future__ import annotations


def test_as_finite_number_passes_through_finite() -> None:
    from extensions.openai_provider.realtime_helpers import as_finite_number

    assert as_finite_number(0.5) == 0.5
    assert as_finite_number(0) == 0.0
    assert as_finite_number(-3.14) == -3.14


def test_as_finite_number_rejects_non_finite() -> None:
    from extensions.openai_provider.realtime_helpers import as_finite_number

    assert as_finite_number(float("inf")) is None
    assert as_finite_number(float("-inf")) is None
    assert as_finite_number(float("nan")) is None
    assert as_finite_number(None) is None
    assert as_finite_number("0.5") is None
    assert as_finite_number(True) is None  # bool excluded explicitly


def test_trim_or_none_returns_stripped_or_none() -> None:
    from extensions.openai_provider.realtime_helpers import trim_or_none

    assert trim_or_none("  hi  ") == "hi"
    assert trim_or_none("") is None
    assert trim_or_none("   ") is None
    assert trim_or_none(None) is None


def test_read_realtime_error_detail_extracts_message() -> None:
    from extensions.openai_provider.realtime_helpers import read_realtime_error_detail

    assert (
        read_realtime_error_detail({"message": "Rate limit exceeded"})
        == "Rate limit exceeded"
    )
    assert (
        read_realtime_error_detail({"type": "invalid_request_error"})
        == "invalid_request_error"
    )
    assert read_realtime_error_detail("simple string") == "simple string"
    assert read_realtime_error_detail(None) == "unknown realtime error"
