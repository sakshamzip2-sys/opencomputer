"""Tests for resume_picker pure-logic helpers (filter + format).

The full-screen Application is hard to unit-test because it depends on
prompt_toolkit's runtime, but the data layer — taking SessionDB rows,
filtering by query, formatting for display — is pure and lives here.
"""
from __future__ import annotations

from opencomputer.cli_ui.resume_picker import (
    SessionRow,
    _clean_label,
    filter_rows,
    format_session_label,
    format_session_preview,
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


# ─── _clean_label ────────────────────────────────────────────────────


def test_clean_label_passes_short_single_line_unchanged():
    assert _clean_label("hello world") == "hello world"


def test_clean_label_collapses_newlines_and_whitespace():
    """Legacy auto-titler shipped titles like 'I understand:\\n\\n1. Use...' —
    those must render as a single readable line."""
    assert _clean_label("I understand:\n\n1. Use foo") == "I understand: 1. Use foo"
    assert _clean_label("a\tb  c\nd") == "a b c d"


def test_clean_label_truncates_with_ellipsis():
    text = "x" * 100
    out = _clean_label(text, max_len=20)
    assert len(out) == 20
    assert out.endswith("…")


def test_clean_label_empty_passes_through():
    assert _clean_label("") == ""


# ─── format_session_label fallback to first_user_message ─────────────


def test_format_session_label_strips_newlines_from_title():
    """Titles with embedded newlines must render as a single line.

    Regression guard for the auto-titler era where titles like
    'I understand:\\n\\n1. Use blogwatcher...' produced multi-line picker
    rows that broke the alt-screen layout.
    """
    row = SessionRow(
        id="x", title="line one\nline two", started_at=0.0, message_count=1
    )
    label = format_session_label(row)
    assert "\n" not in label
    assert label == "line one line two"


def test_format_session_label_uses_first_user_message_when_title_empty():
    """Untitled session + first user message → preview is the headline.

    This is the Claude-Code-parity behaviour: a session that has no
    title yet still shows its conversation context instead of falling
    back to a generic '<cwd-basename> @ HH:MM' label.
    """
    row = SessionRow(
        id="x",
        title="",
        started_at=0.0,
        message_count=2,
        cwd="/Users/saksham/.opencomputer/default",
        first_user_message="Debug the resume picker",
    )
    assert format_session_label(row) == "Debug the resume picker"


def test_format_session_label_prefers_title_over_preview():
    """When both title and preview exist, the title wins (it's the
    intentional name; preview is just context)."""
    row = SessionRow(
        id="x",
        title="my session",
        started_at=0.0,
        message_count=2,
        first_user_message="Debug the resume picker",
    )
    assert format_session_label(row) == "my session"


def test_format_session_label_cleans_first_user_message():
    """Multi-line user prompts collapse to a single readable line."""
    row = SessionRow(
        id="x",
        title="",
        started_at=0.0,
        message_count=2,
        first_user_message="hi\nplease\nhelp",
    )
    assert format_session_label(row) == "hi please help"


def test_format_session_label_still_falls_back_to_cwd_when_no_preview():
    """Backwards-compat: empty title + empty preview + cwd set → cwd label."""
    row = SessionRow(
        id="abc12345",
        title="",
        started_at=1714305600.0,
        message_count=0,
        cwd="/Users/saksham/Vscode/projAlpha",
        first_user_message="",
    )
    label = format_session_label(row)
    assert "projAlpha" in label
    assert "@" in label


# ─── filter_rows matches preview ──────────────────────────────────────


def test_filter_rows_matches_first_user_message():
    """Users typing into the search box should find a session by its
    first-message content even when no title has been set yet."""
    titled = SessionRow(
        id="a", title="foo", started_at=0.0, message_count=1, first_user_message="bar"
    )
    untitled = SessionRow(
        id="b",
        title="",
        started_at=0.0,
        message_count=1,
        first_user_message="debug the picker",
    )
    out = filter_rows([titled, untitled], "picker")
    assert out == [untitled]


# ─── format_session_preview (Claude-Code 3-line layout helper) ────────


def test_format_session_preview_shows_first_user_message_when_title_set():
    """Title is rendered as line 1; preview line 2 shows the user's
    first message so the reader knows what the named session was about.
    """
    row = SessionRow(
        id="x",
        title="OAuth integration",
        started_at=0.0,
        message_count=4,
        cwd="/Users/saksham/work",
        first_user_message="Help me set up OAuth with PKCE",
    )
    assert format_session_preview(row) == "Help me set up OAuth with PKCE"


def test_format_session_preview_falls_back_to_cwd_when_no_title():
    """When no title is set, the headline IS the first_user_message
    (per format_session_label), so showing the same string again would
    be a duplicate. The preview line falls through to the cwd hint so
    line 2 is additive context, not noise."""
    row = SessionRow(
        id="x",
        title="",
        started_at=0.0,
        message_count=1,
        cwd="/Users/saksham/work/projAlpha",
        first_user_message="Help me set up OAuth",
    )
    preview = format_session_preview(row)
    assert "projAlpha" in preview
    # Must NOT duplicate the headline content.
    assert preview != "Help me set up OAuth"


def test_format_session_preview_returns_empty_when_nothing_to_show():
    """Empty title + empty first_user_message + empty cwd → no preview
    text, but the picker still reserves the line slot for uniform row
    height. Returning empty string is the signal."""
    row = SessionRow(
        id="x", title="", started_at=0.0, message_count=0
    )
    assert format_session_preview(row) == ""


def test_format_session_preview_cleans_multiline_user_message():
    """User pasted a multi-paragraph prompt; preview collapses to one line."""
    row = SessionRow(
        id="x",
        title="big paste",
        started_at=0.0,
        message_count=2,
        first_user_message="line one\n\nline two\nline three",
    )
    preview = format_session_preview(row)
    assert "\n" not in preview
    assert preview == "line one line two line three"


def test_format_session_preview_truncates_long_cwd_from_the_left():
    """Long cwd paths (deeply nested) keep the meaningful tail visible."""
    long_cwd = "/Users/saksham/" + "very/deep/nested/path/" * 5 + "actual-project"
    row = SessionRow(
        id="x",
        title="",
        started_at=0.0,
        message_count=1,
        cwd=long_cwd,
        first_user_message="",  # forces cwd fallback
    )
    preview = format_session_preview(row, max_len=40)
    assert len(preview) <= 40
    # Tail must be visible — the project name carries the signal.
    assert "actual-project" in preview
