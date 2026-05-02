"""Tests for SessionSearchTool.

Includes a critical regression test (test_returns_dict_keys_not_attrs) that
guards against the rev-1 bug where hits were accessed via dot-attributes
(h.session_id) instead of dict subscript (h["session_id"]).
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from opencomputer.tools.session_search import SessionSearchTool
from plugin_sdk.core import ToolCall


def _hits():
    return [
        {"session_id": "abc-1234567890", "role": "user", "timestamp": 100, "content": "first hit body"},
        {"session_id": "def-2222222222", "role": "assistant", "timestamp": 200, "content": "second hit body"},
    ]


@pytest.fixture
def tool():
    db = MagicMock()
    db.search_messages.return_value = _hits()
    return SessionSearchTool(db)


def test_returns_dict_keys_not_attrs(tool):
    """Critical regression test for rev 1's `h.session_id` bug."""
    call = ToolCall(id="x", name="SessionSearch", arguments={"query": "first"})
    result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "abc-1234" in result.content  # truncated session_id
    assert "first hit body" in result.content


def test_empty_query_errors(tool):
    call = ToolCall(id="y", name="SessionSearch", arguments={"query": ""})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "query" in result.content.lower()


def test_db_failure_returns_tool_error(tool):
    tool._db_override.search_messages.side_effect = RuntimeError("db locked")
    call = ToolCall(id="z", name="SessionSearch", arguments={"query": "x"})
    result = asyncio.run(tool.execute(call))
    assert result.is_error


def test_limit_passed_through(tool):
    call = ToolCall(id="w", name="SessionSearch", arguments={"query": "x", "limit": 25})
    asyncio.run(tool.execute(call))
    tool._db_override.search_messages.assert_called_once_with("x", limit=25)


@pytest.mark.parametrize(
    "raw_limit,expected",
    [
        (0, 1),          # below minimum → clamps to 1
        (-5, 1),         # negative → clamps to 1
        (100, 50),       # above maximum → clamps to 50
        ("bad", 10),     # non-numeric string → falls back to default
        (None, 10),      # None → falls back to default
        (3.7, 3),        # float coerces via int()
    ],
)
def test_limit_clamping_and_fallback(tool, raw_limit, expected):
    """Verify weird limit inputs map to sane bounded values."""
    call = ToolCall(id="lc", name="SessionSearch", arguments={"query": "x", "limit": raw_limit})
    asyncio.run(tool.execute(call))
    tool._db_override.search_messages.assert_called_once_with("x", limit=expected)


def test_short_session_id_no_misleading_ellipsis(tool):
    """sid format string should only show ellipsis when truncation occurred."""
    tool._db_override.search_messages.return_value = [
        {"session_id": "abc", "role": "user", "content": "x"},
    ]
    call = ToolCall(id="se", name="SessionSearch", arguments={"query": "x"})
    result = asyncio.run(tool.execute(call))
    # Short ID (<=8 chars) — must not have the … truncation indicator
    assert "[abc]" in result.content
    assert "[abc…]" not in result.content


def test_long_session_id_shows_ellipsis(tool):
    """Long IDs are truncated with … indicator."""
    tool._db_override.search_messages.return_value = [
        {"session_id": "abc-12345678-extra", "role": "user", "content": "x"},
    ]
    call = ToolCall(id="le", name="SessionSearch", arguments={"query": "x"})
    result = asyncio.run(tool.execute(call))
    assert "[abc-1234…]" in result.content
