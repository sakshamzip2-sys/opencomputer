"""Tests for Tier 5 advanced tools (3 tools, niche).

Covers:
- Schema correctness
- consent_tier == 5 for all tools
- SANDBOX_HOOK and CONSENT_HOOK markers present
- ExtractSelectedTextTool routes to computer.os.get_selected_text
- ListRunningProcessesTool routes to computer.terminal.run
- ReadSmsMessagesTool routes to computer.sms.get
- Filter param passed correctly to ListRunningProcessesTool
- Optional params handled gracefully
- Wrapper errors propagated
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.oi_capability.tools.tier_5_advanced import (
    ALL_TOOLS,
    ExtractSelectedTextTool,
    ListRunningProcessesTool,
    ReadSmsMessagesTool,
)

from plugin_sdk.core import ToolCall


def _make_wrapper(result=None, raises=None):
    wrapper = MagicMock()
    if raises is not None:
        wrapper.call = AsyncMock(side_effect=raises)
    else:
        wrapper.call = AsyncMock(return_value=result if result is not None else {})
    return wrapper


def _make_call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="t5-call", name=name, arguments=arguments)


class TestAllTier5ToolsList:
    def test_all_tools_has_3_entries(self):
        assert len(ALL_TOOLS) == 3

    def test_all_tools_have_consent_tier_5(self):
        wrapper = _make_wrapper()
        for cls in ALL_TOOLS:
            tool = cls(wrapper=wrapper)
            assert tool.consent_tier == 5, f"{cls.__name__} should have consent_tier=5"

    def test_all_tools_have_sandbox_hook_comment(self):
        for cls in ALL_TOOLS:
            source = inspect.getsource(cls.execute)
            assert "SANDBOX_HOOK" in source, (
                f"{cls.__name__}.execute() is missing # SANDBOX_HOOK comment."
            )

    def test_all_tools_have_consent_hook_comment(self):
        for cls in ALL_TOOLS:
            source = inspect.getsource(cls.execute)
            assert "CONSENT_HOOK" in source, (
                f"{cls.__name__}.execute() is missing # CONSENT_HOOK comment."
            )


class TestExtractSelectedTextTool:
    def test_schema_name(self):
        tool = ExtractSelectedTextTool(wrapper=_make_wrapper())
        assert tool.schema.name == "extract_selected_text"

    def test_schema_no_required_params(self):
        tool = ExtractSelectedTextTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    async def test_execute_calls_os_get_selected_text(self):
        wrapper = _make_wrapper(result="selected text here")
        tool = ExtractSelectedTextTool(wrapper=wrapper)
        call = _make_call("extract_selected_text", {})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once_with("computer.os.get_selected_text", {})
        assert "selected text" in result.content
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("clipboard error"))
        tool = ExtractSelectedTextTool(wrapper=wrapper)
        call = _make_call("extract_selected_text", {})
        result = await tool.execute(call)
        assert result.is_error


class TestListRunningProcessesTool:
    def test_schema_name(self):
        tool = ListRunningProcessesTool(wrapper=_make_wrapper())
        assert tool.schema.name == "list_running_processes"

    def test_schema_no_required_params(self):
        tool = ListRunningProcessesTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    def test_parallel_safe_true(self):
        """Process listing is read-only — can be parallel."""
        tool = ListRunningProcessesTool(wrapper=_make_wrapper())
        assert tool.parallel_safe is True

    async def test_execute_calls_terminal_run(self):
        wrapper = _make_wrapper(result="PID COMMAND\n1 init")
        tool = ListRunningProcessesTool(wrapper=wrapper)
        call = _make_call("list_running_processes", {})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.terminal.run"
        assert not result.is_error

    async def test_execute_passes_filter_in_command(self):
        """When filter is provided, the shell command includes grep."""
        params_captured = {}

        async def mock_call(method, params):
            params_captured.update(params)
            return "filtered output"

        wrapper = MagicMock()
        wrapper.call = mock_call

        tool = ListRunningProcessesTool(wrapper=wrapper)
        call = _make_call("list_running_processes", {"filter": "python"})
        result = await tool.execute(call)

        # The 'code' param should include the filter string
        code = params_captured.get("code", "")
        assert "python" in code.lower()
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("ps error"))
        tool = ListRunningProcessesTool(wrapper=wrapper)
        call = _make_call("list_running_processes", {})
        result = await tool.execute(call)
        assert result.is_error


class TestReadSmsMessagesTool:
    def test_schema_name(self):
        tool = ReadSmsMessagesTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_sms_messages"

    def test_schema_no_required_params(self):
        tool = ReadSmsMessagesTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    async def test_execute_calls_sms_get(self):
        wrapper = _make_wrapper(result=[{"text": "Hello", "date": "2024-01-01"}])
        tool = ReadSmsMessagesTool(wrapper=wrapper)
        call = _make_call("read_sms_messages", {"limit": 10})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.sms.get"
        assert not result.is_error

    async def test_execute_passes_optional_contact(self):
        wrapper = _make_wrapper(result=[])
        tool = ReadSmsMessagesTool(wrapper=wrapper)
        call = _make_call("read_sms_messages", {"contact": "Alice", "limit": 5})
        await tool.execute(call)
        params = wrapper.call.call_args[0][1]
        assert params.get("contact") == "Alice"
        assert params.get("limit") == 5

    async def test_execute_handles_no_params(self):
        wrapper = _make_wrapper(result=[])
        tool = ReadSmsMessagesTool(wrapper=wrapper)
        call = _make_call("read_sms_messages", {})
        result = await tool.execute(call)
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("chat.db access denied"))
        tool = ReadSmsMessagesTool(wrapper=wrapper)
        call = _make_call("read_sms_messages", {})
        result = await tool.execute(call)
        assert result.is_error
