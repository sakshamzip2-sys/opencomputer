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
    tool._db.search_messages.side_effect = RuntimeError("db locked")
    call = ToolCall(id="z", name="SessionSearch", arguments={"query": "x"})
    result = asyncio.run(tool.execute(call))
    assert result.is_error


def test_limit_passed_through(tool):
    call = ToolCall(id="w", name="SessionSearch", arguments={"query": "x", "limit": 25})
    asyncio.run(tool.execute(call))
    tool._db.search_messages.assert_called_once_with("x", limit=25)
