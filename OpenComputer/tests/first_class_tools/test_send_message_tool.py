"""Tests for SendMessageTool — first-class tool for cross-platform agent sends.

Tier 1.B Tool 1 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Wraps the same OutgoingQueue path the MCP `messages_send` server tool uses,
but exposed in the core tool registry so the model reaches for it by reflex.
"""
import json

import pytest

from opencomputer.tools.send_message import SendMessageTool
from plugin_sdk.core import ToolCall


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sessions.db"


@pytest.fixture
def tool(db_path):
    return SendMessageTool(db_path=db_path)


@pytest.mark.asyncio
async def test_send_message_enqueues_to_outgoing_queue(tool, db_path):
    call = ToolCall(
        id="call_1",
        name="SendMessage",
        arguments={
            "platform": "telegram",
            "chat_id": "123456",
            "body": "Hello from the agent",
        },
    )
    result = await tool.execute(call)
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["status"] == "queued"
    assert "id" in payload
    assert payload["platform"] == "telegram"


@pytest.mark.asyncio
async def test_send_message_persists_in_sqlite(tool, db_path):
    call = ToolCall(
        id="c2",
        name="SendMessage",
        arguments={
            "platform": "discord",
            "chat_id": "999",
            "body": "test body",
        },
    )
    await tool.execute(call)
    # Verify the row landed in the DB
    from opencomputer.gateway.outgoing_queue import OutgoingQueue
    queue = OutgoingQueue(db_path)
    pending = list(queue.list_queued())
    assert any(m.body == "test body" and m.platform == "discord" for m in pending)


@pytest.mark.asyncio
async def test_send_message_supports_thread_hint(tool):
    call = ToolCall(
        id="c3",
        name="SendMessage",
        arguments={
            "platform": "telegram",
            "chat_id": "123",
            "body": "from cron",
            "thread_hint": "cron:morning-briefing",
        },
    )
    result = await tool.execute(call)
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload.get("thread_hint") == "cron:morning-briefing"


@pytest.mark.asyncio
async def test_send_message_missing_platform_returns_error(tool):
    call = ToolCall(
        id="c4",
        name="SendMessage",
        arguments={"chat_id": "x", "body": "y"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "platform" in result.content.lower()


@pytest.mark.asyncio
async def test_send_message_missing_chat_id_returns_error(tool):
    call = ToolCall(
        id="c5",
        name="SendMessage",
        arguments={"platform": "telegram", "body": "y"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "chat_id" in result.content.lower()


@pytest.mark.asyncio
async def test_send_message_empty_body_returns_error(tool):
    call = ToolCall(
        id="c6",
        name="SendMessage",
        arguments={"platform": "telegram", "chat_id": "1", "body": ""},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "body" in result.content.lower()


def test_schema_is_well_formed(tool):
    s = tool.schema
    assert s.name == "SendMessage"
    assert "platform" in s.parameters["properties"]
    assert "chat_id" in s.parameters["properties"]
    assert "body" in s.parameters["properties"]
    required = s.parameters.get("required", [])
    assert "platform" in required
    assert "chat_id" in required
    assert "body" in required


def test_schema_to_anthropic_format(tool):
    """Smoke: schema converts to Anthropic format without crashing."""
    fmt = tool.schema.to_anthropic_format()
    assert fmt["name"] == "SendMessage"
    assert "input_schema" in fmt


@pytest.mark.asyncio
async def test_send_message_truncates_oversized_body(tool):
    """Bodies over a sane limit are rejected with a clear error
    rather than silently truncated, since truncation could send
    half a sentence to a channel."""
    huge = "x" * 50_000
    call = ToolCall(
        id="c7",
        name="SendMessage",
        arguments={"platform": "telegram", "chat_id": "1", "body": huge},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "too long" in result.content.lower() or "exceeds" in result.content.lower()
