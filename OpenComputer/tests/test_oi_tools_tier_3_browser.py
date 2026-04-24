"""Tests for Tier 3 browser tools (3 tools).

Covers:
- Schema correctness
- consent_tier == 3
- ReadBrowserHistoryTool routes to terminal.run (sqlite3)
- ReadBrowserBookmarksTool routes to terminal.run
- ReadBrowserDomTool routes to browser.go_to_url + browser.get_page_content
- URL parameter required for ReadBrowserDomTool
- Wrapper errors propagated as is_error=True
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from extensions.oi_capability.tools.tier_3_browser import (
    ALL_TOOLS,
    ReadBrowserBookmarksTool,
    ReadBrowserDomTool,
    ReadBrowserHistoryTool,
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
    return ToolCall(id="t3-call", name=name, arguments=arguments)


class TestAllTier3ToolsList:
    def test_all_tools_has_3_entries(self):
        assert len(ALL_TOOLS) == 3

    def test_all_tools_have_consent_tier_3(self):
        wrapper = _make_wrapper()
        for cls in ALL_TOOLS:
            tool = cls(wrapper=wrapper)
            assert tool.consent_tier == 3, f"{cls.__name__} should have consent_tier=3"


class TestReadBrowserHistoryTool:
    def test_schema_name(self):
        tool = ReadBrowserHistoryTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_browser_history"

    def test_schema_no_required_params(self):
        tool = ReadBrowserHistoryTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    def test_schema_has_browser_and_limit(self):
        tool = ReadBrowserHistoryTool(wrapper=_make_wrapper())
        props = tool.schema.parameters["properties"]
        assert "browser" in props
        assert "limit" in props

    async def test_execute_calls_terminal_run(self):
        wrapper = _make_wrapper(result="url|title|timestamp\n")
        tool = ReadBrowserHistoryTool(wrapper=wrapper)
        call = _make_call("read_browser_history", {"limit": 10})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.terminal.run"
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("sqlite locked"))
        tool = ReadBrowserHistoryTool(wrapper=wrapper)
        call = _make_call("read_browser_history", {})
        result = await tool.execute(call)
        assert result.is_error


class TestReadBrowserBookmarksTool:
    def test_schema_name(self):
        tool = ReadBrowserBookmarksTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_browser_bookmarks"

    def test_schema_no_required_params(self):
        tool = ReadBrowserBookmarksTool(wrapper=_make_wrapper())
        assert tool.schema.parameters["required"] == []

    async def test_execute_calls_terminal_run(self):
        wrapper = _make_wrapper(result='{"roots": {}}')
        tool = ReadBrowserBookmarksTool(wrapper=wrapper)
        call = _make_call("read_browser_bookmarks", {"browser": "chrome"})
        result = await tool.execute(call)
        method = wrapper.call.call_args[0][0]
        assert method == "computer.terminal.run"
        assert not result.is_error

    async def test_execute_with_default_params(self):
        wrapper = _make_wrapper(result="bookmarks data")
        tool = ReadBrowserBookmarksTool(wrapper=wrapper)
        call = _make_call("read_browser_bookmarks", {})
        result = await tool.execute(call)
        assert not result.is_error


class TestReadBrowserDomTool:
    def test_schema_name(self):
        tool = ReadBrowserDomTool(wrapper=_make_wrapper())
        assert tool.schema.name == "read_browser_dom"

    def test_schema_requires_url(self):
        tool = ReadBrowserDomTool(wrapper=_make_wrapper())
        assert "url" in tool.schema.parameters["required"]

    async def test_execute_navigates_then_gets_content(self):
        """Should call go_to_url then get_page_content."""
        call_sequence = []

        async def mock_call(method, params):
            call_sequence.append(method)
            return "page content"

        wrapper = MagicMock()
        wrapper.call = mock_call

        tool = ReadBrowserDomTool(wrapper=wrapper)
        call = _make_call("read_browser_dom", {"url": "https://example.com"})
        result = await tool.execute(call)

        assert "computer.browser.go_to_url" in call_sequence
        assert "computer.browser.get_page_content" in call_sequence
        assert call_sequence.index("computer.browser.go_to_url") < call_sequence.index("computer.browser.get_page_content")
        assert not result.is_error

    async def test_execute_error_propagation(self):
        wrapper = _make_wrapper(raises=RuntimeError("selenium error"))
        tool = ReadBrowserDomTool(wrapper=wrapper)
        call = _make_call("read_browser_dom", {"url": "https://example.com"})
        result = await tool.execute(call)
        assert result.is_error

    async def test_execute_passes_url_correctly(self):
        navigate_params = {}

        async def capture_call(method, params):
            if method == "computer.browser.go_to_url":
                navigate_params.update(params)
            return "content"

        wrapper = MagicMock()
        wrapper.call = capture_call

        tool = ReadBrowserDomTool(wrapper=wrapper)
        call = _make_call("read_browser_dom", {"url": "https://opencomputer.ai"})
        await tool.execute(call)

        assert navigate_params.get("url") == "https://opencomputer.ai"
