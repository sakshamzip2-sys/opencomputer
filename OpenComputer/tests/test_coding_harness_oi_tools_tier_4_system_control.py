"""Tests for Tier 4 system control tools (4 tools, MUTATING).

Key assertions:
- consent_tier == 4 for all tools
- SANDBOX_HOOK comment present in execute() source (verified via inspect)
- CONSENT_HOOK comment present
- EditFileTool routes to computer.files.edit
- RunShellTool routes to computer.terminal.run
- RunAppleScriptTool routes to computer.os.run_applescript
- InjectKeyboardTool routes to computer.keyboard.write
- Required params enforced by schema
- Wrapper errors propagated as is_error=True
- parallel_safe == False for all Tier 4 tools (mutations are sequential)
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.coding_harness.oi_bridge.tools.tier_4_system_control import (
    ALL_TOOLS,
    EditFileTool,
    InjectKeyboardTool,
    RunAppleScriptTool,
    RunShellTool,
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
    return ToolCall(id="t4-call", name=name, arguments=arguments)


class TestAllTier4ToolsList:
    def test_all_tools_has_4_entries(self):
        assert len(ALL_TOOLS) == 4

    def test_all_tools_have_consent_tier_4(self):
        wrapper = _make_wrapper()
        for cls in ALL_TOOLS:
            tool = cls(wrapper=wrapper)
            assert tool.consent_tier == 4, f"{cls.__name__} should have consent_tier=4"

    def test_all_tools_not_parallel_safe(self):
        """Tier 4 mutations must be sequential — parallel_safe=False."""
        wrapper = _make_wrapper()
        for cls in ALL_TOOLS:
            tool = cls(wrapper=wrapper)
            assert tool.parallel_safe is False, f"{cls.__name__} should have parallel_safe=False"

    def test_all_tools_have_sandbox_hook_comment(self):
        """Every Tier 4 execute() must have # SANDBOX_HOOK marker for Phase 5 wiring."""
        for cls in ALL_TOOLS:
            source = inspect.getsource(cls.execute)
            assert "SANDBOX_HOOK" in source, (
                f"{cls.__name__}.execute() is missing # SANDBOX_HOOK comment. "
                "This marker is required for Session A's Phase 5 to wire SandboxStrategy."
            )

    def test_all_tools_have_capability_claims(self):
        """Every Tier 4 tool must declare capability_claims (PR-3: replaces # CONSENT_HOOK marker).

        F1 ConsentGate enforces at dispatch via the class-level capability_claims attr —
        no in-execute() call needed. The old # CONSENT_HOOK marker has been replaced.
        """
        from plugin_sdk.consent import CapabilityClaim  # noqa: PLC0415
        for cls in ALL_TOOLS:
            assert hasattr(cls, "capability_claims"), (
                f"{cls.__name__} is missing capability_claims class attr"
            )
            assert len(cls.capability_claims) >= 1, (
                f"{cls.__name__}.capability_claims must have at least one CapabilityClaim"
            )
            assert all(isinstance(c, CapabilityClaim) for c in cls.capability_claims), (
                f"{cls.__name__}.capability_claims must contain CapabilityClaim instances"
            )


class TestEditFileTool:
    def test_schema_name(self):
        tool = EditFileTool(wrapper=_make_wrapper())
        assert tool.schema.name == "edit_file"

    def test_schema_requires_path_original_replacement(self):
        tool = EditFileTool(wrapper=_make_wrapper())
        required = tool.schema.parameters["required"]
        assert "path" in required
        assert "original_text" in required
        assert "replacement_text" in required

    async def test_execute_calls_files_edit(self):
        wrapper = _make_wrapper(result={"status": "edited"})
        tool = EditFileTool(wrapper=wrapper)
        call = _make_call("edit_file", {
            "path": "/tmp/test.txt",
            "original_text": "old",
            "replacement_text": "new",
        })
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.files.edit"
        params = wrapper.call.call_args[0][1]
        assert params["path"] == "/tmp/test.txt"
        assert params["original_text"] == "old"
        assert params["replacement_text"] == "new"
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("file not found"))
        tool = EditFileTool(wrapper=wrapper)
        call = _make_call("edit_file", {"path": "/x", "original_text": "a", "replacement_text": "b"})
        result = await tool.execute(call)
        assert result.is_error


class TestRunShellTool:
    def test_schema_name(self):
        tool = RunShellTool(wrapper=_make_wrapper())
        assert tool.schema.name == "run_shell"

    def test_schema_requires_command(self):
        tool = RunShellTool(wrapper=_make_wrapper())
        assert "command" in tool.schema.parameters["required"]

    async def test_execute_calls_terminal_run(self):
        wrapper = _make_wrapper(result="stdout output")
        tool = RunShellTool(wrapper=wrapper)
        call = _make_call("run_shell", {"command": "ls -la"})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.terminal.run"
        params = wrapper.call.call_args[0][1]
        assert params["language"] == "shell"
        assert params["code"] == "ls -la"
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("command failed"))
        tool = RunShellTool(wrapper=wrapper)
        call = _make_call("run_shell", {"command": "bad_command"})
        result = await tool.execute(call)
        assert result.is_error

    def test_sandbox_hook_in_source(self):
        source = inspect.getsource(RunShellTool.execute)
        assert "SANDBOX_HOOK" in source


class TestRunAppleScriptTool:
    def test_schema_name(self):
        tool = RunAppleScriptTool(wrapper=_make_wrapper())
        assert tool.schema.name == "run_applescript"

    def test_schema_requires_script(self):
        tool = RunAppleScriptTool(wrapper=_make_wrapper())
        assert "script" in tool.schema.parameters["required"]

    async def test_execute_calls_os_run_applescript(self):
        wrapper = _make_wrapper(result="AppleScript result")
        tool = RunAppleScriptTool(wrapper=wrapper)
        call = _make_call("run_applescript", {"script": "tell application \"Finder\" to open"})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.os.run_applescript"
        params = wrapper.call.call_args[0][1]
        assert "script" in params
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("applescript error"))
        tool = RunAppleScriptTool(wrapper=wrapper)
        call = _make_call("run_applescript", {"script": "bad script"})
        result = await tool.execute(call)
        assert result.is_error


class TestInjectKeyboardTool:
    def test_schema_name(self):
        tool = InjectKeyboardTool(wrapper=_make_wrapper())
        assert tool.schema.name == "inject_keyboard"

    def test_schema_requires_text(self):
        tool = InjectKeyboardTool(wrapper=_make_wrapper())
        assert "text" in tool.schema.parameters["required"]

    async def test_execute_calls_keyboard_write(self):
        wrapper = _make_wrapper(result=None)
        tool = InjectKeyboardTool(wrapper=wrapper)
        call = _make_call("inject_keyboard", {"text": "Hello World", "interval": 0.05})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.keyboard.write"
        params = wrapper.call.call_args[0][1]
        assert params["text"] == "Hello World"
        assert params["interval"] == 0.05
        assert not result.is_error

    async def test_execute_uses_default_interval(self):
        wrapper = _make_wrapper(result=None)
        tool = InjectKeyboardTool(wrapper=wrapper)
        call = _make_call("inject_keyboard", {"text": "test"})
        await tool.execute(call)
        params = wrapper.call.call_args[0][1]
        assert params["interval"] == 0.05  # default

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("keyboard error"))
        tool = InjectKeyboardTool(wrapper=wrapper)
        call = _make_call("inject_keyboard", {"text": "hello"})
        result = await tool.execute(call)
        assert result.is_error
