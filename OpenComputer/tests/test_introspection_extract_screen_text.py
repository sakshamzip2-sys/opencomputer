"""tests/test_introspection_extract_screen_text.py"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from extensions.coding_harness.introspection.tools import ExtractScreenTextTool

from plugin_sdk.core import ToolCall


@pytest.mark.asyncio
async def test_returns_joined_ocr_text():
    with patch(
        "extensions.coding_harness.introspection.tools.ocr_text_from_screen",
        return_value="Hello World\nLine 2",
    ):
        tool = ExtractScreenTextTool()
        result = await tool.execute(ToolCall(id="t1", name="extract_screen_text", arguments={}))

    assert not result.is_error
    assert "Hello World" in result.content
    assert "Line 2" in result.content


@pytest.mark.asyncio
async def test_returns_empty_string_when_screen_is_blank():
    with patch(
        "extensions.coding_harness.introspection.tools.ocr_text_from_screen",
        return_value="",
    ):
        tool = ExtractScreenTextTool()
        result = await tool.execute(ToolCall(id="t1", name="extract_screen_text", arguments={}))

    assert not result.is_error
    assert result.content == ""


@pytest.mark.asyncio
async def test_handles_ocr_runtime_error():
    """rapidocr import or model load can fail (corrupted ONNX, no internet on first run, etc.)."""
    with patch(
        "extensions.coding_harness.introspection.tools.ocr_text_from_screen",
        side_effect=RuntimeError("rapidocr model not loaded"),
    ):
        tool = ExtractScreenTextTool()
        result = await tool.execute(ToolCall(id="t1", name="extract_screen_text", arguments={}))

    assert result.is_error
    assert "rapidocr" in result.content.lower()


@pytest.mark.asyncio
async def test_handles_mss_failure_inside_ocr():
    """mss can fail with no display (Linux server, headless container)."""
    with patch(
        "extensions.coding_harness.introspection.tools.ocr_text_from_screen",
        side_effect=Exception("XOpenDisplay failed"),
    ):
        tool = ExtractScreenTextTool()
        result = await tool.execute(ToolCall(id="t1", name="extract_screen_text", arguments={}))

    assert result.is_error
    assert "XOpenDisplay" in result.content


@pytest.mark.asyncio
async def test_capability_claim_namespace():
    claims = ExtractScreenTextTool.capability_claims
    assert claims[0].capability_id == "introspection.extract_screen_text"


@pytest.mark.asyncio
async def test_parallel_safe_is_false():
    """RapidOCR instance is ~200MB; parallel calls cause memory pressure."""
    assert ExtractScreenTextTool.parallel_safe is False


def test_ocr_module_lazy_imports_rapidocr():
    """Verify ocr.py does NOT pull rapidocr at module-load time.

    Rationale: ocr_text_from_screen costs ~5s (model load) on first call;
    users who never call it shouldn't pay startup penalty.
    """
    from pathlib import Path

    from extensions.coding_harness.introspection import ocr as ocr_mod
    src = Path(ocr_mod.__file__).read_text()
    top_level_lines = [line for line in src.splitlines() if line and not line.startswith((" ", "\t"))]
    for line in top_level_lines:
        assert "rapidocr_onnxruntime" not in line, f"top-level import of rapidocr leaks startup cost: {line!r}"
