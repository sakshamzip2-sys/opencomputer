"""Tests for the browser-control plugin (T1+T2 of 2026-04-28 plan).

Mocks Playwright entirely — no browser binary is launched. Covers:

* Capability namespace + tier on each of the 5 BaseTool subclasses.
* BrowserError raised when playwright is not installed.
* Schema name + parameters shape for each tool.
* navigate_and_snapshot happy path (mocked Playwright module).
* Error path — page.goto throws → snap.error populated, ToolResult is_error=True.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from extensions.browser_control.browser import BrowserError, PageSnapshot
from extensions.browser_control.tools import (
    ALL_TOOLS,
    BrowserClickTool,
    BrowserFillTool,
    BrowserNavigateTool,
    BrowserScrapeTool,
    BrowserSnapshotTool,
)

from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall


def test_all_tools_count():
    assert len(ALL_TOOLS) == 5


def test_capability_namespaces():
    expected = {
        BrowserNavigateTool: ("browser.navigate", ConsentTier.EXPLICIT),
        BrowserClickTool: ("browser.click", ConsentTier.EXPLICIT),
        BrowserFillTool: ("browser.fill", ConsentTier.EXPLICIT),
        BrowserSnapshotTool: ("browser.snapshot", ConsentTier.IMPLICIT),
        BrowserScrapeTool: ("browser.scrape", ConsentTier.IMPLICIT),
    }
    for cls, (cap_id, tier) in expected.items():
        claims = cls.capability_claims
        assert len(claims) == 1
        assert claims[0].capability_id == cap_id
        assert claims[0].tier_required == tier


@pytest.mark.asyncio
async def test_navigate_missing_url_returns_error():
    tool = BrowserNavigateTool()
    result = await tool.execute(ToolCall(id="t1", name="browser_navigate", arguments={}))
    assert result.is_error
    assert "missing url" in result.content.lower()


@pytest.mark.asyncio
async def test_navigate_happy_path_mocked():
    snap = PageSnapshot(
        url="https://example.com", title="Example", accessibility_tree="root",
        text_content="hello", error="",
    )
    with patch("extensions.browser_control.tools.navigate_and_snapshot",
               new_callable=AsyncMock, return_value=snap):
        tool = BrowserNavigateTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_navigate",
                                              arguments={"url": "https://example.com"}))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["url"] == "https://example.com"
    assert payload["title"] == "Example"


@pytest.mark.asyncio
async def test_navigate_browser_error_returned():
    with patch("extensions.browser_control.tools.navigate_and_snapshot",
               new_callable=AsyncMock, side_effect=BrowserError("playwright missing")):
        tool = BrowserNavigateTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_navigate",
                                              arguments={"url": "https://example.com"}))
    assert result.is_error
    assert "playwright" in result.content.lower()


@pytest.mark.asyncio
async def test_navigate_snap_error_returned():
    snap = PageSnapshot(url="x", title="", accessibility_tree="", text_content="",
                        error="navigation failed: timeout")
    with patch("extensions.browser_control.tools.navigate_and_snapshot",
               new_callable=AsyncMock, return_value=snap):
        tool = BrowserNavigateTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_navigate",
                                              arguments={"url": "x"}))
    assert result.is_error
    assert "navigation failed" in result.content


@pytest.mark.asyncio
async def test_click_missing_args_returns_error():
    tool = BrowserClickTool()
    result = await tool.execute(ToolCall(id="t1", name="browser_click", arguments={"url": "x"}))
    assert result.is_error


@pytest.mark.asyncio
async def test_fill_happy_path_mocked():
    snap = PageSnapshot(url="x", title="t", accessibility_tree="", text_content="",
                        error="")
    with patch("extensions.browser_control.tools.fill_input",
               new_callable=AsyncMock, return_value=snap):
        tool = BrowserFillTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_fill",
                                              arguments={"url": "x", "selector": "input", "value": "hi"}))
    assert not result.is_error


@pytest.mark.asyncio
async def test_scrape_with_selector():
    snap = PageSnapshot(url="x", title="t", accessibility_tree="", text_content="row1\nrow2",
                        error="")
    with patch("extensions.browser_control.tools.scrape_url",
               new_callable=AsyncMock, return_value=snap) as mock:
        tool = BrowserScrapeTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_scrape",
                                              arguments={"url": "x", "css_selector": ".item"}))
    assert not result.is_error
    payload = json.loads(result.content)
    assert "row1" in payload["text_content"]
    mock.assert_called_once_with("x", ".item")


def test_browser_error_when_playwright_missing():
    """browser._import_playwright raises BrowserError with install hint."""
    from extensions.browser_control.browser import _import_playwright

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(BrowserError, match="playwright"):
            _import_playwright()


def test_schemas_have_required_fields():
    """All 5 tools have schema with name + parameters.required."""
    for cls in ALL_TOOLS:
        tool = cls()
        schema = tool.schema
        assert schema.name.startswith("browser_")
        assert schema.parameters["type"] == "object"
        assert "required" in schema.parameters


def test_snapshot_schema_required_url():
    tool = BrowserSnapshotTool()
    assert tool.schema.parameters["required"] == ["url"]


def test_click_schema_required_url_and_selector():
    tool = BrowserClickTool()
    assert tool.schema.parameters["required"] == ["url", "selector"]


def test_fill_schema_required_url_selector_value():
    tool = BrowserFillTool()
    assert tool.schema.parameters["required"] == ["url", "selector", "value"]
