"""tests/test_skill_evolution_session_metrics.py — adapter for SessionDB → SessionMetrics."""
from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest
from extensions.skill_evolution.session_metrics import (
    SessionMetrics,
    ToolCallSummary,
    compute_session_metrics,
)


def _msg(role: str, content="", tool_calls=None, is_error=False):
    """Build a Message-like mock."""
    m = MagicMock()
    m.role = role
    m.content = content
    m.tool_calls = tool_calls
    m.is_error = is_error
    return m


def test_session_metrics_is_frozen():
    metrics = SessionMetrics(session_id="s1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        metrics.session_id = "s2"


def test_tool_call_summary_is_frozen():
    tc = ToolCallSummary(is_error=True, turn_index=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tc.is_error = False


def test_compute_returns_none_for_missing_session():
    db = MagicMock()
    db.get_messages.return_value = []
    assert compute_session_metrics(db, "missing") is None


def test_compute_returns_none_when_db_raises():
    db = MagicMock()
    db.get_messages.side_effect = RuntimeError("DB closed")
    assert compute_session_metrics(db, "x") is None


def test_compute_aggregates_user_message_chars():
    db = MagicMock()
    db.get_messages.return_value = [
        _msg("user", content="Please help me port this cpp module"),
        _msg("assistant", content="Sure, let me read it"),
        _msg("user", content="here's the file path: /tmp/foo.cpp"),
    ]
    metrics = compute_session_metrics(db, "s1")

    assert metrics is not None
    assert metrics.session_id == "s1"
    assert metrics.turn_count == 3
    assert "port this cpp module" in metrics.user_messages_concat
    assert "/tmp/foo.cpp" in metrics.user_messages_concat
    assert metrics.user_messages_total_chars == len(metrics.user_messages_concat)


def test_compute_handles_multimodal_content_blocks():
    """User messages with content as a list of blocks (text + image) — extract text only."""
    db = MagicMock()
    db.get_messages.return_value = [
        _msg("user", content=[
            {"type": "text", "text": "What's in this chart?"},
            {"type": "image", "source": {"data": "<base64>"}},
        ]),
    ]
    metrics = compute_session_metrics(db, "s1")
    assert metrics is not None
    assert "What's in this chart?" in metrics.user_messages_concat
    assert "base64" not in metrics.user_messages_concat


def test_compute_extracts_tool_calls_with_is_error_flag():
    """Assistant tool_calls + tool result messages produce ToolCallSummary entries."""
    db = MagicMock()
    db.get_messages.return_value = [
        _msg("user", content="run the tests"),
        _msg("assistant", content="ok", tool_calls=[MagicMock(id="t1"), MagicMock(id="t2")]),
        _msg("tool", content="2 failures", is_error=True),
        _msg("tool", content="now passing", is_error=False),
    ]
    metrics = compute_session_metrics(db, "s1")

    assert metrics is not None
    # 2 assistant tool_calls + 2 tool results = 4 ToolCallSummary entries
    assert len(metrics.tool_calls) == 4

    error_calls = [tc for tc in metrics.tool_calls if tc.is_error]
    assert len(error_calls) == 1


def test_compute_recovery_pattern_detectable():
    """A session that errors then recovers should have at least one
    successful tool call with turn_index > the errored one's turn_index."""
    db = MagicMock()
    db.get_messages.return_value = [
        _msg("user", content="x" * 200),
        _msg("assistant", content="trying", tool_calls=[MagicMock(id="t1")]),
        _msg("tool", content="boom", is_error=True),
        _msg("assistant", content="retry", tool_calls=[MagicMock(id="t2")]),
        _msg("tool", content="ok", is_error=False),
    ]
    metrics = compute_session_metrics(db, "s1")
    assert metrics is not None
    # Find the last error turn_index and last success turn_index
    last_error = max((tc.turn_index for tc in metrics.tool_calls if tc.is_error), default=-1)
    last_success = max((tc.turn_index for tc in metrics.tool_calls if not tc.is_error), default=-1)
    assert last_success > last_error  # recovery present


def test_compute_handles_non_string_content_gracefully():
    """Garbage content shape (e.g. None, dict without text) doesn't crash."""
    db = MagicMock()
    db.get_messages.return_value = [
        _msg("user", content=None),
        _msg("user", content=42),  # numeric — definitely not text
        _msg("user", content="real text"),
    ]
    metrics = compute_session_metrics(db, "s1")
    assert metrics is not None
    assert "real text" in metrics.user_messages_concat
    # Only "real text" should be counted; len computed correctly
    assert metrics.user_messages_total_chars == len("real text")


def test_compute_with_zero_messages_returns_none():
    db = MagicMock()
    db.get_messages.return_value = []
    assert compute_session_metrics(db, "empty") is None


def test_metrics_is_consumable_by_pattern_detector():
    """Smoke: SessionMetrics has the attributes the detector reads."""
    metrics = SessionMetrics(
        session_id="s1",
        turn_count=5,
        user_messages_total_chars=200,
        user_messages_concat="port cpp module to python",
        tool_calls=(ToolCallSummary(is_error=False, turn_index=1),),
    )
    # attribute access — what is_candidate_session does internally
    assert int(getattr(metrics, "user_messages_total_chars", 0) or 0) == 200
    assert str(getattr(metrics, "user_messages_concat", "") or "") == "port cpp module to python"
    assert list(getattr(metrics, "tool_calls", []) or []) == [
        ToolCallSummary(is_error=False, turn_index=1)
    ]
