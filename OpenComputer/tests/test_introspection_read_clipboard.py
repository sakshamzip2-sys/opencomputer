"""tests/test_introspection_read_clipboard.py"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from extensions.coding_harness.introspection.tools import ReadClipboardOnceTool

from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_clipboard_text():
    with patch("extensions.coding_harness.introspection.tools.pyperclip.paste", return_value="hello"):
        tool = ReadClipboardOnceTool()
        result = await tool.execute(ToolCall(id="t1", name="read_clipboard_once", arguments={}))

    assert not result.is_error
    assert result.content == "hello"


@pytest.mark.asyncio
async def test_returns_empty_string_when_clipboard_empty():
    with patch("extensions.coding_harness.introspection.tools.pyperclip.paste", return_value=""):
        tool = ReadClipboardOnceTool()
        result = await tool.execute(ToolCall(id="t1", name="read_clipboard_once", arguments={}))

    assert not result.is_error
    assert result.content == ""


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ReadClipboardOnceTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "introspection.read_clipboard_once"


@pytest.mark.asyncio
async def test_handles_pyperclip_error():
    """pyperclip raises pyperclip.PyperclipException on Linux when xclip/xsel missing."""
    import pyperclip
    with patch(
        "extensions.coding_harness.introspection.tools.pyperclip.paste",
        side_effect=pyperclip.PyperclipException("xclip not installed"),
    ):
        tool = ReadClipboardOnceTool()
        result = await tool.execute(ToolCall(id="t2", name="read_clipboard_once", arguments={}))

    assert result.is_error
    assert "xclip" in result.content


@pytest.mark.asyncio
async def test_handles_unexpected_exception():
    with patch(
        "extensions.coding_harness.introspection.tools.pyperclip.paste",
        side_effect=RuntimeError("kaboom"),
    ):
        tool = ReadClipboardOnceTool()
        result = await tool.execute(ToolCall(id="t3", name="read_clipboard_once", arguments={}))

    assert result.is_error
    assert "kaboom" in result.content
