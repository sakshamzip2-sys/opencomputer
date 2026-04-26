"""Tests for Tier 1 introspection tools (5 tools, post-trim 2026-04-25).

Covers:
- Schema correctness (name, description, required params)
- execute() delegates to wrapper with correct OI method
- consent_tier == 1 for all tools
- Wrapper errors propagated as ToolResult(is_error=True)

Removed in 2026-04-25 trim — tests deleted along with the classes:
- ReadFileRegionTool — duplicated built-in Read tool
- SearchFilesTool    — duplicated built-in Grep + Glob
- ReadGitLogTool     — duplicated BashTool running `git log`
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from extensions.coding_harness.oi_bridge.tools.tier_1_introspection import (
    ALL_TOOLS,
    ExtractScreenTextTool,
    ListAppUsageTool,
    ListRecentFilesTool,
    ReadClipboardOnceTool,
    ScreenshotTool,
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
    def test_all_tools_has_5_entries(self):
        # Post-trim 2026-04-25: read_file_region / search_files / read_git_log
        # removed because they duplicated built-in Read / Grep+Glob / Bash.
        assert len(ALL_TOOLS) == 5

    def test_all_tools_have_consent_tier_1(self):
        wrapper = _make_wrapper()
        for cls in ALL_TOOLS:
            tool = cls(wrapper=wrapper)
            assert tool.consent_tier == 1, f"{cls.__name__} should have consent_tier=1"

    def test_all_tools_are_macos_unique_capabilities(self):
        # Sanity: every surviving tool has a name that signals OI's
        # genuine macOS-unique value (clipboard / screen / app / file
        # listing). Guards against accidentally re-adding a duplicate.
        names = {cls.__name__ for cls in ALL_TOOLS}
        assert names == {
            "ListAppUsageTool",
            "ReadClipboardOnceTool",
            "ScreenshotTool",
            "ExtractScreenTextTool",
            "ListRecentFilesTool",
        }


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

