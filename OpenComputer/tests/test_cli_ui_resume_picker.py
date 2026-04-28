"""Tests for resume_picker pure-logic helpers (filter + format).

The full-screen Application is hard to unit-test because it depends on
prompt_toolkit's runtime, but the data layer — taking SessionDB rows,
filtering by query, formatting for display — is pure and lives here.
"""
from __future__ import annotations

from opencomputer.cli_ui.resume_picker import (
    SessionRow,
    filter_rows,
    format_time_ago,
)


def test_filter_rows_empty_query_returns_all():
    rows = [
        SessionRow(id="abc123", title="hello", started_at=1714281600.0, message_count=4),
        SessionRow(id="def456", title="bye", started_at=1714281660.0, message_count=2),
    ]
    assert filter_rows(rows, "") == rows


def test_filter_rows_substring_match_on_title_case_insensitive():
    a = SessionRow(id="a", title="Architecture review", started_at=0.0, message_count=1)
    b = SessionRow(id="b", title="bug triage", started_at=0.0, message_count=1)
    out = filter_rows([a, b], "arch")
    assert out == [a]
    out = filter_rows([a, b], "TRIAGE")
    assert out == [b]


def test_filter_rows_no_match_returns_empty():
    a = SessionRow(id="a", title="hello", started_at=0.0, message_count=1)
    assert filter_rows([a], "zzz") == []


def test_filter_rows_matches_id_prefix():
    """If the query looks like a session id prefix, match against id too —
    so users can paste a partial UUID from logs."""
    a = SessionRow(id="abc12345-1111", title="hello", started_at=0.0, message_count=1)
    b = SessionRow(id="def67890-2222", title="bye", started_at=0.0, message_count=1)
    out = filter_rows([a, b], "abc12")
    assert out == [a]


def test_format_time_ago_seconds():
    now = 1714305600.0
    assert format_time_ago(now - 5, now=now) == "5 seconds ago"


def test_format_time_ago_minutes():
    now = 1714305600.0
    assert format_time_ago(now - 12 * 60, now=now) == "12 minutes ago"


def test_format_time_ago_hours():
    now = 1714305600.0
    assert format_time_ago(now - 3 * 3600, now=now) == "3 hours ago"


def test_format_time_ago_days():
    now = 1714305600.0
    assert format_time_ago(now - 2 * 86400, now=now) == "2 days ago"


def test_format_time_ago_handles_invalid_value():
    assert format_time_ago("not-a-number") == "unknown"  # type: ignore[arg-type]
    assert format_time_ago(None) == "unknown"  # type: ignore[arg-type]


def test_format_time_ago_just_now():
    now = 1714305600.0
    assert format_time_ago(now - 0.5, now=now) == "just now"


def test_format_time_ago_singular_plural():
    now = 1714305600.0
    assert format_time_ago(now - 1, now=now) == "1 second ago"
    assert format_time_ago(now - 60, now=now) == "1 minute ago"
    assert format_time_ago(now - 3600, now=now) == "1 hour ago"
    assert format_time_ago(now - 86400, now=now) == "1 day ago"
