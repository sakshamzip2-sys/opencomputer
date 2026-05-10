"""Tests for MCP error/output redaction wiring (Hermes parity)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.mcp.client import MCPTool


class _FakeBlock:
    """Minimal MCP content block stub — has a ``.text`` attribute."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResult:
    def __init__(self, content: list[_FakeBlock], is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


def _make_tool(session: MagicMock) -> MCPTool:
    """Build an MCPTool with the mocked session."""
    tool = MCPTool.__new__(MCPTool)
    tool.session = session
    tool.server_name = "fake-server"
    tool.tool_name = "fake-tool"
    tool.parameters = {"type": "object", "properties": {}}
    tool._description = "fake"
    return tool


def test_mcp_success_output_is_redacted():
    """A GitHub PAT in successful MCP output gets redacted before LLM sees it."""
    leaky_text = "Here is your token: ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_FakeResult(content=[_FakeBlock(leaky_text)])
    )
    tool = _make_tool(session)
    from plugin_sdk.core import ToolCall

    call = ToolCall(id="t1", name="fake-server__fake-tool", arguments={})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is False
    assert "ghp_" not in result.content
    assert "<GITHUB_PAT_REDACTED>" in result.content


def test_mcp_error_message_is_redacted():
    """An exception message containing an Anthropic key gets redacted."""
    session = MagicMock()
    leaky_err = (
        "auth failure with sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    session.call_tool = AsyncMock(side_effect=RuntimeError(leaky_err))
    tool = _make_tool(session)
    from plugin_sdk.core import ToolCall

    call = ToolCall(id="t2", name="fake-server__fake-tool", arguments={})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "sk-ant-api03-" not in result.content
    assert "<ANTHROPIC_KEY_REDACTED>" in result.content


def test_mcp_bearer_token_redacted_in_output():
    leaky = "Authorization: Bearer abc123def456ghi789jkl012mno345"
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_FakeResult(content=[_FakeBlock(leaky)])
    )
    tool = _make_tool(session)
    from plugin_sdk.core import ToolCall

    call = ToolCall(id="t3", name="fake-server__fake-tool", arguments={})
    result = asyncio.run(tool.execute(call))
    # Bearer token gets matched by either Bearer pattern or generic redaction.
    assert "abc123def456ghi789jkl012mno345" not in result.content


def test_mcp_clean_output_passes_through():
    """No secrets → output unchanged."""
    clean = "All systems operational. Processed 42 records."
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_FakeResult(content=[_FakeBlock(clean)])
    )
    tool = _make_tool(session)
    from plugin_sdk.core import ToolCall

    call = ToolCall(id="t4", name="fake-server__fake-tool", arguments={})
    result = asyncio.run(tool.execute(call))
    assert result.is_error is False
    assert result.content == clean
