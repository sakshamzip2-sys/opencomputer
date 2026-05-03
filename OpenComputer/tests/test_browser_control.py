"""Tests for the browser-control plugin (T1+T2 of 2026-04-28 plan + Hermes-parity Batch 1 2026-05-01).

Mocks Playwright entirely — no browser binary is launched. Covers:

* Capability namespace + tier on each BaseTool subclass.
* BrowserError raised when playwright is not installed.
* Schema name + parameters shape for each tool.
* navigate_and_snapshot happy path (mocked Playwright module).
* Error path — page.goto throws → snap.error populated, ToolResult is_error=True.
* Hermes-parity tools — scroll, back, press, get_images, vision, console.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from extensions.browser_control._browser_session import BrowserError, PageSnapshot
from extensions.browser_control._tools import (
    ALL_TOOLS,
    BrowserBackTool,
    BrowserClickTool,
    BrowserConsoleTool,
    BrowserFillTool,
    BrowserGetImagesTool,
    BrowserNavigateTool,
    BrowserPressTool,
    BrowserScrapeTool,
    BrowserScrollTool,
    BrowserSnapshotTool,
    BrowserVisionTool,
)

from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall


def test_all_tools_count():
    # 5 base + 6 Hermes-parity Batch 1
    assert len(ALL_TOOLS) == 11


def test_capability_namespaces():
    expected = {
        BrowserNavigateTool: ("browser.navigate", ConsentTier.EXPLICIT),
        BrowserClickTool: ("browser.click", ConsentTier.EXPLICIT),
        BrowserFillTool: ("browser.fill", ConsentTier.EXPLICIT),
        BrowserSnapshotTool: ("browser.snapshot", ConsentTier.IMPLICIT),
        BrowserScrapeTool: ("browser.scrape", ConsentTier.IMPLICIT),
        BrowserScrollTool: ("browser.scroll", ConsentTier.IMPLICIT),
        BrowserBackTool: ("browser.navigate", ConsentTier.EXPLICIT),
        BrowserPressTool: ("browser.fill", ConsentTier.EXPLICIT),
        BrowserGetImagesTool: ("browser.scrape", ConsentTier.IMPLICIT),
        BrowserVisionTool: ("browser.screenshot", ConsentTier.EXPLICIT),
        BrowserConsoleTool: ("browser.scrape", ConsentTier.IMPLICIT),
    }
    for cls, (cap_id, tier) in expected.items():
        claims = cls.capability_claims
        assert len(claims) == 1, f"{cls.__name__} should have 1 capability claim"
        assert claims[0].capability_id == cap_id, f"{cls.__name__} cap_id mismatch"
        assert claims[0].tier_required == tier, f"{cls.__name__} tier mismatch"


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
    with patch("extensions.browser_control._tools.navigate_and_snapshot",
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
    with patch("extensions.browser_control._tools.navigate_and_snapshot",
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
    with patch("extensions.browser_control._tools.navigate_and_snapshot",
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
    with patch("extensions.browser_control._tools.fill_input",
               new_callable=AsyncMock, return_value=snap):
        tool = BrowserFillTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_fill",
                                              arguments={"url": "x", "selector": "input", "value": "hi"}))
    assert not result.is_error


@pytest.mark.asyncio
async def test_scrape_with_selector():
    snap = PageSnapshot(url="x", title="t", accessibility_tree="", text_content="row1\nrow2",
                        error="")
    with patch("extensions.browser_control._tools.scrape_url",
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
    from extensions.browser_control._browser_session import _import_playwright

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(BrowserError, match="playwright"):
            _import_playwright()


def test_schemas_have_required_fields():
    """All tools have schema with name + parameters.required."""
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


# ─── Hermes-parity Batch 1 (2026-05-01) ────────────────────────────


@pytest.mark.asyncio
async def test_scroll_happy_path_mocked():
    snap = PageSnapshot(url="x", title="t", accessibility_tree="", text_content="bottom",
                        error="")
    with patch("extensions.browser_control._tools.scroll_page",
               new_callable=AsyncMock, return_value=snap) as mock:
        tool = BrowserScrollTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_scroll",
                                              arguments={"url": "x", "direction": "bottom"}))
    assert not result.is_error
    mock.assert_called_once_with("x", direction="bottom", amount_px=500)


@pytest.mark.asyncio
async def test_scroll_missing_url_returns_error():
    tool = BrowserScrollTool()
    result = await tool.execute(ToolCall(id="t1", name="browser_scroll", arguments={}))
    assert result.is_error
    assert "missing url" in result.content.lower()


@pytest.mark.asyncio
async def test_back_happy_path_mocked():
    snap = PageSnapshot(url="x", title="prev", accessibility_tree="", text_content="",
                        error="")
    with patch("extensions.browser_control._tools.go_back",
               new_callable=AsyncMock, return_value=snap):
        tool = BrowserBackTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_back",
                                              arguments={"url": "x"}))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["title"] == "prev"


@pytest.mark.asyncio
async def test_back_no_history_error():
    snap = PageSnapshot(url="x", title="", accessibility_tree="", text_content="",
                        error="no back history available in this session")
    with patch("extensions.browser_control._tools.go_back",
               new_callable=AsyncMock, return_value=snap):
        tool = BrowserBackTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_back",
                                              arguments={"url": "x"}))
    assert result.is_error
    assert "no back history" in result.content


@pytest.mark.asyncio
async def test_press_with_selector():
    snap = PageSnapshot(url="x", title="t", accessibility_tree="", text_content="",
                        error="")
    with patch("extensions.browser_control._tools.press_key",
               new_callable=AsyncMock, return_value=snap) as mock:
        tool = BrowserPressTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_press",
                                              arguments={"url": "x", "key": "Enter",
                                                         "selector": "input"}))
    assert not result.is_error
    mock.assert_called_once_with("x", "Enter", selector="input")


@pytest.mark.asyncio
async def test_press_missing_key_returns_error():
    tool = BrowserPressTool()
    result = await tool.execute(ToolCall(id="t1", name="browser_press",
                                          arguments={"url": "x"}))
    assert result.is_error
    assert "key" in result.content.lower()


@pytest.mark.asyncio
async def test_get_images_happy_path():
    payload = {
        "url": "x", "title": "t", "image_count": 2,
        "images": [
            {"src": "a.png", "alt": "a", "width": 100, "height": 100},
            {"src": "b.png", "alt": "b", "width": 200, "height": 200},
        ],
    }
    with patch("extensions.browser_control._tools.get_images",
               new_callable=AsyncMock, return_value=payload):
        tool = BrowserGetImagesTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_get_images",
                                              arguments={"url": "x"}))
    assert not result.is_error
    decoded = json.loads(result.content)
    assert decoded["image_count"] == 2
    assert decoded["images"][0]["src"] == "a.png"


@pytest.mark.asyncio
async def test_get_images_error_dict_returned():
    with patch("extensions.browser_control._tools.get_images",
               new_callable=AsyncMock, return_value={"url": "x", "error": "boom", "images": []}):
        tool = BrowserGetImagesTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_get_images",
                                              arguments={"url": "x"}))
    assert result.is_error
    assert "boom" in result.content


@pytest.mark.asyncio
async def test_vision_happy_path():
    payload = {
        "url": "x", "title": "t",
        "image_base64": "iVBORw0KGgo=", "image_format": "png",
        "image_size_bytes": 8,
    }
    with patch("extensions.browser_control._tools.vision_screenshot",
               new_callable=AsyncMock, return_value=payload):
        tool = BrowserVisionTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_vision",
                                              arguments={"url": "x"}))
    assert not result.is_error
    decoded = json.loads(result.content)
    assert decoded["image_format"] == "png"
    assert decoded["image_base64"] == "iVBORw0KGgo="


@pytest.mark.asyncio
async def test_vision_error_dict_returned():
    with patch("extensions.browser_control._tools.vision_screenshot",
               new_callable=AsyncMock, return_value={"url": "x", "error": "screenshot failed"}):
        tool = BrowserVisionTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_vision",
                                              arguments={"url": "x"}))
    assert result.is_error
    assert "screenshot failed" in result.content


@pytest.mark.asyncio
async def test_console_happy_path():
    payload = {
        "url": "x", "title": "t", "message_count": 2,
        "messages": [
            {"type": "log", "text": "hi", "location": ""},
            {"type": "error", "text": "boom", "location": "x.js"},
        ],
    }
    with patch("extensions.browser_control._tools.get_console_messages",
               new_callable=AsyncMock, return_value=payload):
        tool = BrowserConsoleTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_console",
                                              arguments={"url": "x"}))
    assert not result.is_error
    decoded = json.loads(result.content)
    assert decoded["message_count"] == 2
    assert decoded["messages"][1]["type"] == "error"


@pytest.mark.asyncio
async def test_press_browser_error_returned():
    with patch("extensions.browser_control._tools.press_key",
               new_callable=AsyncMock, side_effect=BrowserError("playwright missing")):
        tool = BrowserPressTool()
        result = await tool.execute(ToolCall(id="t1", name="browser_press",
                                              arguments={"url": "x", "key": "Enter"}))
    assert result.is_error
    assert "playwright" in result.content.lower()


def test_scroll_schema_url_required():
    tool = BrowserScrollTool()
    assert tool.schema.parameters["required"] == ["url"]
    direction_enum = tool.schema.parameters["properties"]["direction"]["enum"]
    assert set(direction_enum) == {"up", "down", "top", "bottom"}


def test_press_schema_url_and_key_required():
    tool = BrowserPressTool()
    assert tool.schema.parameters["required"] == ["url", "key"]


def test_vision_schema_url_required():
    tool = BrowserVisionTool()
    assert tool.schema.parameters["required"] == ["url"]
