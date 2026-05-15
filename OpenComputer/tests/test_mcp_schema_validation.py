"""Gap C — MCP tool-arg schema validation (mcp-openclaw-port follow-up).

MCPTool.execute now validates ``call.arguments`` against the tool's
declared ``inputSchema`` BEFORE dispatching to ClientSession.call_tool.
A failed validation returns a clear ``ToolResult(is_error=True)``
naming the offending field — saves a round-trip to the MCP server
just to get a generic "invalid params" 500 back.

Validation policy:

* Schema is the tool's manifest ``inputSchema`` (mapped to OC's
  ``MCPTool.parameters``).
* ``additionalProperties`` is set to False by default? **No** — we
  honor what the MCP server declared. Servers that allow extra
  properties keep working. Validation is jsonschema's Draft 7 (the
  flavour MCP servers use).
* On schema-missing / non-dict schema, validation is skipped (the
  tool is permissive, fall through to MCP server).

Tests cover required-field-missing, wrong-type, unexpected-extra
(when additionalProperties=False), and the schema-missing pass-through.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from opencomputer.mcp.client import MCPTool
from opencomputer.mcp.schema_validation import (
    SchemaValidationError,
    validate_tool_arguments,
)
from plugin_sdk.core import ToolCall


# ─── pure validator ──────────────────────────────────────────────


def test_validate_passes_on_clean_args() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "required": ["name"],
    }
    # No exception → validates
    validate_tool_arguments({"name": "alice"}, schema)
    validate_tool_arguments({"name": "alice", "age": 30}, schema)


def test_validate_rejects_missing_required() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_tool_arguments({}, schema)
    assert "name" in str(ei.value).lower()


def test_validate_rejects_wrong_type() -> None:
    schema = {
        "type": "object",
        "properties": {"age": {"type": "integer"}},
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_tool_arguments({"age": "thirty"}, schema)
    assert "age" in str(ei.value).lower() or "integer" in str(ei.value).lower()


def test_validate_honors_additional_properties_false() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    with pytest.raises(SchemaValidationError):
        validate_tool_arguments({"a": "x", "extra": "y"}, schema)


def test_validate_allows_additional_properties_default_true() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        # additionalProperties unspecified → defaults to True per JSON Schema
    }
    validate_tool_arguments({"a": "x", "extra": "y"}, schema)


def test_validate_no_schema_is_pass_through() -> None:
    """Empty schema or None is permissive — caller wins fall through."""
    validate_tool_arguments({"anything": 1}, {})
    validate_tool_arguments({"anything": 1}, None)


def test_validate_non_object_schema_is_pass_through() -> None:
    """A scalar / non-object schema means the tool doesn't take args of
    that shape — we permit and let the MCP server be the authority."""
    validate_tool_arguments({"x": 1}, {"type": "string"})


def test_validate_nested_required_propagates_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        "required": ["user"],
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_tool_arguments({"user": {}}, schema)
    msg = str(ei.value).lower()
    assert "name" in msg


def test_validate_error_message_includes_field_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "integer"},
            },
        },
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_tool_arguments({"items": [1, "two", 3]}, schema)
    # jsonschema reports the path; our wrapper preserves it
    assert "items" in str(ei.value)


# ─── MCPTool integration ─────────────────────────────────────────


def _make_tool(parameters: dict[str, Any]) -> MCPTool:
    session = MagicMock()
    tool = MCPTool.__new__(MCPTool)
    tool.server_name = "srv"
    tool.tool_name = "echo"
    tool.description = ""
    tool.parameters = parameters
    tool.session = session
    tool.timeout = 30.0
    tool.session_loop = None
    return tool


def test_execute_rejects_bad_args_without_calling_session() -> None:
    tool = _make_tool({
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    })
    call = ToolCall(id="c1", name="srv__echo", arguments={})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "name" in result.content.lower() or "required" in result.content.lower()
    # session.call_tool was never invoked
    tool.session.call_tool.assert_not_called()


def test_execute_passes_good_args_through() -> None:
    tool = _make_tool({
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    })
    # Stub call_tool to return a content-shaped object
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text="hello", type="text")]
    fake_result.isError = False

    async def _call_tool(name=None, arguments=None):
        return fake_result

    tool.session.call_tool = _call_tool
    call = ToolCall(id="c1", name="srv__echo", arguments={"name": "alice"})
    result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "hello" in result.content


def test_execute_passthrough_on_missing_schema() -> None:
    """When parameters is empty, validation is skipped — MCP server decides."""
    tool = _make_tool({})
    fake_result = MagicMock()
    fake_result.content = [MagicMock(text="ok", type="text")]
    fake_result.isError = False

    async def _call_tool(name=None, arguments=None):
        return fake_result

    tool.session.call_tool = _call_tool
    call = ToolCall(id="c1", name="srv__echo", arguments={"whatever": 1})
    result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert "ok" in result.content
