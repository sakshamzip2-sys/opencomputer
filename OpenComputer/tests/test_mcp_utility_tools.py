"""T1 — MCP utility tools (list_resources / read_resource / list_prompts / get_prompt).

Hermes-doc parity: when an MCP server's ``initialize`` reply advertises
the ``resources`` or ``prompts`` capability, register helper tools the
agent can call to enumerate + fetch them.

Tools are namespaced ``<server>__<utility>`` matching the existing
:class:`opencomputer.mcp.client.MCPTool` convention.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.mcp.client import (
    _build_utility_tools,
    _MCPGetPromptTool,
    _MCPListPromptsTool,
    _MCPListResourcesTool,
    _MCPReadResourceTool,
)
from plugin_sdk.core import ToolCall


def _make_session() -> MagicMock:
    """Build a mock MCP ClientSession.

    Uses ``SimpleNamespace`` for nested objects so attributes hold real
    string values (MagicMock's ``name`` kwarg is reserved for the mock's
    repr, not for setting a ``.name`` attribute).
    """
    session = MagicMock()
    session.list_resources = AsyncMock(
        return_value=SimpleNamespace(
            resources=[
                SimpleNamespace(
                    uri="file:///foo.txt",
                    name="foo",
                    description="A file",
                    mimeType="text/plain",
                ),
            ]
        )
    )
    session.read_resource = AsyncMock(
        return_value=SimpleNamespace(
            contents=[SimpleNamespace(uri="file:///foo.txt", text="hello world")]
        )
    )
    session.list_prompts = AsyncMock(
        return_value=SimpleNamespace(
            prompts=[
                SimpleNamespace(name="welcome", description="A greeting", arguments=[]),
            ]
        )
    )
    session.get_prompt = AsyncMock(
        return_value=SimpleNamespace(messages=[{"role": "user", "content": "hello"}])
    )
    return session


def test_resources_capability_builds_two_tools():
    session = _make_session()
    capabilities = {"resources": {}, "prompts": None}
    tools = _build_utility_tools("fs", session, capabilities)
    names = [t.schema.name for t in tools]
    assert "fs__list_resources" in names
    assert "fs__read_resource" in names
    assert "fs__list_prompts" not in names
    assert "fs__get_prompt" not in names


def test_prompts_capability_builds_two_tools():
    session = _make_session()
    capabilities = {"resources": None, "prompts": {}}
    tools = _build_utility_tools("git", session, capabilities)
    names = [t.schema.name for t in tools]
    assert "git__list_prompts" in names
    assert "git__get_prompt" in names
    assert "git__list_resources" not in names


def test_no_capabilities_builds_nothing():
    session = _make_session()
    assert _build_utility_tools("empty", session, {}) == []
    assert _build_utility_tools("none", session, None) == []


def test_both_capabilities_build_four_tools():
    session = _make_session()
    capabilities = {"resources": {}, "prompts": {}}
    tools = _build_utility_tools("full", session, capabilities)
    assert len(tools) == 4
    assert all(hasattr(t, "schema") and hasattr(t, "execute") for t in tools)


@pytest.mark.asyncio
async def test_list_resources_invokes_session():
    session = _make_session()
    tool = _MCPListResourcesTool(server_name="fs", session=session)
    result = await tool.execute(ToolCall(id="t1", name="fs__list_resources", arguments={}))
    assert session.list_resources.await_count == 1
    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload[0]["uri"] == "file:///foo.txt"


@pytest.mark.asyncio
async def test_read_resource_invokes_session_with_uri():
    session = _make_session()
    tool = _MCPReadResourceTool(server_name="fs", session=session)
    result = await tool.execute(
        ToolCall(id="t2", name="fs__read_resource", arguments={"uri": "file:///foo.txt"})
    )
    session.read_resource.assert_awaited_with("file:///foo.txt")
    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["contents"][0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_read_resource_missing_uri_errors():
    session = _make_session()
    tool = _MCPReadResourceTool(server_name="fs", session=session)
    result = await tool.execute(
        ToolCall(id="t3", name="fs__read_resource", arguments={})
    )
    assert result.is_error is True
    assert "uri" in result.content.lower()


@pytest.mark.asyncio
async def test_list_prompts_invokes_session():
    session = _make_session()
    tool = _MCPListPromptsTool(server_name="git", session=session)
    result = await tool.execute(ToolCall(id="t4", name="git__list_prompts", arguments={}))
    assert session.list_prompts.await_count == 1
    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload[0]["name"] == "welcome"


@pytest.mark.asyncio
async def test_get_prompt_invokes_session():
    session = _make_session()
    tool = _MCPGetPromptTool(server_name="git", session=session)
    result = await tool.execute(
        ToolCall(id="t5", name="git__get_prompt", arguments={"name": "welcome"})
    )
    session.get_prompt.assert_awaited_with("welcome", arguments=None)
    assert result.is_error is False


@pytest.mark.asyncio
async def test_get_prompt_missing_name_errors():
    session = _make_session()
    tool = _MCPGetPromptTool(server_name="git", session=session)
    result = await tool.execute(
        ToolCall(id="t6", name="git__get_prompt", arguments={})
    )
    assert result.is_error is True
    assert "name" in result.content.lower()


@pytest.mark.asyncio
async def test_session_failure_returns_error_result():
    """Tools should never raise — failures land in ToolResult.is_error."""
    session = MagicMock()
    session.list_resources = AsyncMock(side_effect=RuntimeError("boom"))
    tool = _MCPListResourcesTool(server_name="bad", session=session)
    result = await tool.execute(
        ToolCall(id="t7", name="bad__list_resources", arguments={})
    )
    assert result.is_error is True
    assert "boom" in result.content
