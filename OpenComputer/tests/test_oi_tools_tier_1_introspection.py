"""Tests for Tier 1 introspection tools (8 tools).

Covers:
- Schema correctness (name, description, required params)
- execute() delegates to wrapper with correct OI method
- consent_tier == 1 for all tools
- ReadGitLogTool uses inline git (no wrapper call)
- ReadGitLogTool handles git not found gracefully
- ReadGitLogTool handles git error returncode
- Wrapper errors propagated as ToolResult(is_error=True)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.tools.tier_1_introspection import (
    ALL_TOOLS,
    ExtractScreenTextTool,
    ListAppUsageTool,
    ListRecentFilesTool,
    ReadClipboardOnceTool,
    ReadFileRegionTool,
    ReadGitLogTool,
    ScreenshotTool,
    SearchFilesTool,
)

from plugin_sdk.core import ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wrapper(result=None, raises=None):
    wrapper = MagicMock()
    if raises is not None:
        wrapper.call = AsyncMock(side_effect=raises)
    else:
        wrapper.call = AsyncMock(return_value=result or {})
    return wrapper


def _make_call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="test-call-id", name=name, arguments=arguments)


# ---------------------------------------------------------------------------
# ALL_TOOLS list
# ---------------------------------------------------------------------------

class TestAllToolsList:
    def test_all_tools_has_8_entries(self):
        assert len(ALL_TOOLS) == 8

    def test_all_tools_have_consent_tier_1(self):
        wrapper = _make_wrapper()
        for cls in ALL_TOOLS:
            tool = cls(wrapper=wrapper)
            assert tool.consent_tier == 1, f"{cls.__name__} should have consent_tier=1"


# ---------------------------------------------------------------------------
# ReadFileRegionTool
# ---------------------------------------------------------------------------

class TestReadFileRegionTool:
    def test_schema_name(self):
        tool = ReadFileRegionTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_file_region"

    def test_schema_has_path_required(self):
        tool = ReadFileRegionTool(wrapper=_make_wrapper())
        assert "path" in tool.schema.parameters["required"]

    async def test_execute_calls_wrapper_with_correct_method(self):
        wrapper = _make_wrapper(result={"content": "hello"})
        tool = ReadFileRegionTool(wrapper=wrapper)
        call = _make_call("read_file_region", {"path": "/tmp/test.txt", "offset": 0, "length": 100})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once_with("computer.files.read", call.arguments)
        assert not result.is_error

    async def test_execute_returns_error_on_wrapper_exception(self):
        wrapper = _make_wrapper(raises=RuntimeError("subprocess error"))
        tool = ReadFileRegionTool(wrapper=wrapper)
        call = _make_call("read_file_region", {"path": "/tmp/test.txt"})
        result = await tool.execute(call)
        assert result.is_error
        assert "Error" in result.content


# ---------------------------------------------------------------------------
# ListAppUsageTool
# ---------------------------------------------------------------------------

class TestListAppUsageTool:
    def test_schema_name(self):
        tool = ListAppUsageTool(wrapper=_make_wrapper())
        assert tool.schema.name == "list_app_usage"

    def test_schema_no_required_params(self):
        tool = ListAppUsageTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    async def test_execute_calls_terminal_run(self):
        wrapper = _make_wrapper(result="PID TTY\n1 ?")
        tool = ListAppUsageTool(wrapper=wrapper)
        call = _make_call("list_app_usage", {"hours": 4})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once()
        method_called = wrapper.call.call_args[0][0]
        assert method_called == "computer.terminal.run"
        assert not result.is_error


# ---------------------------------------------------------------------------
# ReadClipboardOnceTool
# ---------------------------------------------------------------------------

class TestReadClipboardOnceTool:
    def test_schema_name(self):
        tool = ReadClipboardOnceTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_clipboard_once"

    async def test_execute_calls_clipboard_view(self):
        wrapper = _make_wrapper(result="clipboard text")
        tool = ReadClipboardOnceTool(wrapper=wrapper)
        call = _make_call("read_clipboard_once", {})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once_with("computer.clipboard.view", {})
        assert "clipboard text" in result.content


# ---------------------------------------------------------------------------
# ScreenshotTool
# ---------------------------------------------------------------------------

class TestScreenshotTool:
    def test_schema_name(self):
        tool = ScreenshotTool(wrapper=_make_wrapper())
        assert tool.schema.name == "screenshot"

    def test_schema_has_optional_quadrant(self):
        tool = ScreenshotTool(wrapper=_make_wrapper())
        assert "quadrant" in tool.schema.parameters["properties"]
        assert tool.schema.parameters["required"] == []

    async def test_execute_calls_display_view(self):
        wrapper = _make_wrapper(result="base64png...")
        tool = ScreenshotTool(wrapper=wrapper)
        call = _make_call("screenshot", {})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once_with("computer.display.view", {})
        assert not result.is_error

    async def test_execute_passes_quadrant_param(self):
        wrapper = _make_wrapper(result="base64png...")
        tool = ScreenshotTool(wrapper=wrapper)
        call = _make_call("screenshot", {"quadrant": "top-left"})
        await tool.execute(call)
        _, params = wrapper.call.call_args[0]
        assert params.get("quadrant") == "top-left"


# ---------------------------------------------------------------------------
# ExtractScreenTextTool
# ---------------------------------------------------------------------------

class TestExtractScreenTextTool:
    def test_schema_name(self):
        tool = ExtractScreenTextTool(wrapper=_make_wrapper())
        assert tool.schema.name == "extract_screen_text"

    async def test_execute_calls_display_ocr(self):
        wrapper = _make_wrapper(result="Hello World")
        tool = ExtractScreenTextTool(wrapper=wrapper)
        call = _make_call("extract_screen_text", {})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once_with("computer.display.ocr", {})
        assert "Hello World" in result.content


# ---------------------------------------------------------------------------
# ListRecentFilesTool
# ---------------------------------------------------------------------------

class TestListRecentFilesTool:
    def test_schema_name(self):
        tool = ListRecentFilesTool(wrapper=_make_wrapper())
        assert tool.schema.name == "list_recent_files"

    async def test_execute_calls_terminal_run(self):
        wrapper = _make_wrapper(result="/tmp/foo.txt\n")
        tool = ListRecentFilesTool(wrapper=wrapper)
        call = _make_call("list_recent_files", {"hours": 2})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.terminal.run"
        assert not result.is_error


# ---------------------------------------------------------------------------
# SearchFilesTool
# ---------------------------------------------------------------------------

class TestSearchFilesTool:
    def test_schema_name(self):
        tool = SearchFilesTool(wrapper=_make_wrapper())
        assert tool.schema.name == "search_files"

    def test_schema_requires_query(self):
        tool = SearchFilesTool(wrapper=_make_wrapper())
        assert "query" in tool.schema.parameters["required"]

    async def test_execute_calls_files_search(self):
        wrapper = _make_wrapper(result=["file1.py", "file2.py"])
        tool = SearchFilesTool(wrapper=wrapper)
        call = _make_call("search_files", {"query": "agent"})
        result = await tool.execute(call)
        wrapper.call.assert_awaited_once_with("computer.files.search", call.arguments)
        assert not result.is_error


# ---------------------------------------------------------------------------
# ReadGitLogTool — carve-out: no OI subprocess
# ---------------------------------------------------------------------------

class TestReadGitLogTool:
    def test_schema_name(self):
        tool = ReadGitLogTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_git_log"

    def test_schema_no_required_params(self):
        tool = ReadGitLogTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    async def test_execute_does_not_call_wrapper(self):
        """ReadGitLogTool must NOT call the OI subprocess wrapper — it's the carve-out tool."""
        wrapper = _make_wrapper()
        tool = ReadGitLogTool(wrapper=wrapper)
        call = _make_call("read_git_log", {"repo_path": ".", "limit": 5})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123 Initial commit\n", stderr="")
            result = await tool.execute(call)

        # Wrapper should NOT have been called
        wrapper.call.assert_not_awaited()
        assert not result.is_error

    async def test_execute_returns_git_log_output(self):
        wrapper = _make_wrapper()
        tool = ReadGitLogTool(wrapper=wrapper)
        call = _make_call("read_git_log", {"repo_path": ".", "limit": 3, "format": "oneline"})

        expected_output = "abc123 feat: add feature\ndef456 fix: bugfix\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=expected_output, stderr="")
            result = await tool.execute(call)

        assert expected_output in result.content
        assert not result.is_error

    async def test_execute_handles_git_not_found(self):
        wrapper = _make_wrapper()
        tool = ReadGitLogTool(wrapper=wrapper)
        call = _make_call("read_git_log", {})

        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = await tool.execute(call)

        assert result.is_error
        assert "git" in result.content.lower()

    async def test_execute_handles_git_error_returncode(self):
        wrapper = _make_wrapper()
        tool = ReadGitLogTool(wrapper=wrapper)
        call = _make_call("read_git_log", {"repo_path": "/not/a/repo"})

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128, stdout="", stderr="fatal: not a git repository"
            )
            result = await tool.execute(call)

        assert result.is_error
        assert "fatal" in result.content.lower() or "error" in result.content.lower()

    async def test_execute_handles_timeout(self):
        wrapper = _make_wrapper()
        tool = ReadGitLogTool(wrapper=wrapper)
        call = _make_call("read_git_log", {})

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            result = await tool.execute(call)

        assert result.is_error
        assert "timed out" in result.content.lower() or "timeout" in result.content.lower()
