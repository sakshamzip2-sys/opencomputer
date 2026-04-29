"""Tests for the RecallScreen tool — agent-callable screen-history query."""
from __future__ import annotations

import asyncio
import time

from plugin_sdk.core import ToolCall

from extensions.screen_awareness.recall_tool import RecallScreenTool
from extensions.screen_awareness.ring_buffer import ScreenCapture, ScreenRingBuffer


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_schema_name_and_required_args():
    tool = RecallScreenTool(ring_buffer=ScreenRingBuffer(max_size=5))
    schema = tool.schema
    assert schema.name == "RecallScreen"
    assert "window_seconds" in schema.parameters["properties"]
    assert schema.parameters.get("required", []) == []


def test_recall_empty_buffer_returns_explanatory_text():
    tool = RecallScreenTool(ring_buffer=ScreenRingBuffer(max_size=5))
    call = ToolCall(id="t1", name="RecallScreen", arguments={})
    result = _run(tool.execute(call))
    assert result.is_error is False
    assert "no screen captures" in result.content.lower()


def test_recall_returns_most_recent_first():
    buf = ScreenRingBuffer(max_size=5)
    now = time.time()
    buf.append(ScreenCapture(captured_at=now - 5, text="older", sha256="o", trigger="user_message", session_id="s"))
    buf.append(ScreenCapture(captured_at=now, text="newer", sha256="n", trigger="user_message", session_id="s"))
    tool = RecallScreenTool(ring_buffer=buf)
    call = ToolCall(id="t1", name="RecallScreen", arguments={})
    result = _run(tool.execute(call))
    newer_pos = result.content.find("newer")
    older_pos = result.content.find("older")
    assert 0 <= newer_pos < older_pos


def test_recall_window_seconds_filter():
    buf = ScreenRingBuffer(max_size=5)
    now = time.time()
    buf.append(ScreenCapture(captured_at=now - 100, text="old", sha256="o", trigger="user_message", session_id="s"))
    buf.append(ScreenCapture(captured_at=now - 1, text="recent", sha256="r", trigger="user_message", session_id="s"))
    tool = RecallScreenTool(ring_buffer=buf)
    call = ToolCall(id="t1", name="RecallScreen", arguments={"window_seconds": 10})
    result = _run(tool.execute(call))
    assert "recent" in result.content
    assert "old" not in result.content
