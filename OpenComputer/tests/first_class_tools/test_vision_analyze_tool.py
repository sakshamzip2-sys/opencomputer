"""Tests for VisionAnalyzeTool — first-class vision-analysis tool.

Tier 1.B Tool 2 (per docs/refs/hermes-agent/2026-04-28-major-gaps.md).

Tests use httpx.MockTransport to stub the vision API + image fetch endpoints.
"""
import base64
from unittest.mock import patch

import httpx
import pytest

from opencomputer.tools.vision_analyze import VisionAnalyzeTool
from plugin_sdk.core import ToolCall

# Minimum-valid PNG: 8-byte signature + IHDR + IEND. ~73 bytes.
_PNG_MAGIC = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
    0, 0, 0, 13, 73, 72, 68, 82,  # IHDR length + type
    0, 0, 0, 1, 0, 0, 0, 1,  # 1x1 dimensions
    8, 0, 0, 0, 0, 0x37, 0x6E, 0xF9, 0x24,  # bit depth + CRC
    0, 0, 0, 0, 73, 69, 78, 68,  # IEND length + type
    0xAE, 0x42, 0x60, 0x82,  # CRC
])

_JPEG_MAGIC = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"x" * 30
_FAKE_HTML = b"<html><body>not an image</body></html>"


def _mock_anthropic_response(text: str) -> dict:
    """Shape mimicking Anthropic Messages API response."""
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def _make_mock_transport(image_bytes: bytes | None, response_text: str = "A photograph of a cat sitting on a sofa, looking at the camera") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "anthropic.com" in request.url.host:
            return httpx.Response(200, json=_mock_anthropic_response(response_text))
        # Image fetch
        if image_bytes is None:
            return httpx.Response(404)
        return httpx.Response(200, content=image_bytes, headers={"content-type": "image/png"})

    return httpx.MockTransport(handler)


@pytest.fixture
def tool():
    return VisionAnalyzeTool(api_key="test-key")


@pytest.mark.asyncio
async def test_analyze_with_base64_input(tool, monkeypatch):
    transport = _make_mock_transport(image_bytes=None)  # no fetch path
    monkeypatch.setattr(
        "opencomputer.tools.vision_analyze._make_async_client",
        lambda timeout=60.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    b64 = base64.b64encode(_PNG_MAGIC).decode()
    call = ToolCall(
        id="c1",
        name="VisionAnalyze",
        arguments={"image_base64": b64, "prompt": "What's in this image?"},
    )
    result = await tool.execute(call)
    assert not result.is_error
    assert "cat" in result.content.lower() or "photograph" in result.content.lower()


@pytest.mark.asyncio
async def test_analyze_with_url_input(tool, monkeypatch):
    transport = _make_mock_transport(image_bytes=_PNG_MAGIC)
    monkeypatch.setattr(
        "opencomputer.tools.vision_analyze._make_async_client",
        lambda timeout=60.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c2",
        name="VisionAnalyze",
        arguments={
            "image_url": "https://example.com/cat.png",
            "prompt": "Describe",
        },
    )
    result = await tool.execute(call)
    assert not result.is_error
    assert "photograph" in result.content.lower() or "cat" in result.content.lower()


@pytest.mark.asyncio
async def test_no_image_input_returns_error(tool):
    call = ToolCall(
        id="c3",
        name="VisionAnalyze",
        arguments={"prompt": "describe"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "image" in result.content.lower()


@pytest.mark.asyncio
async def test_unsafe_url_blocked(tool, monkeypatch):
    """is_safe_url rejects private/internal IPs."""
    call = ToolCall(
        id="c4",
        name="VisionAnalyze",
        arguments={"image_url": "http://169.254.169.254/metadata"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "unsafe" in result.content.lower() or "blocked" in result.content.lower()


@pytest.mark.asyncio
async def test_non_image_content_rejected(tool, monkeypatch):
    """Magic-byte sniff rejects HTML / text / non-image content."""
    transport = _make_mock_transport(image_bytes=_FAKE_HTML)
    monkeypatch.setattr(
        "opencomputer.tools.vision_analyze._make_async_client",
        lambda timeout=60.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c5",
        name="VisionAnalyze",
        arguments={"image_url": "https://example.com/bad"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "not an image" in result.content.lower() or "magic" in result.content.lower()


@pytest.mark.asyncio
async def test_default_prompt_used_when_omitted(tool, monkeypatch):
    transport = _make_mock_transport(image_bytes=None)
    monkeypatch.setattr(
        "opencomputer.tools.vision_analyze._make_async_client",
        lambda timeout=60.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    b64 = base64.b64encode(_PNG_MAGIC).decode()
    call = ToolCall(
        id="c6",
        name="VisionAnalyze",
        arguments={"image_base64": b64},
    )
    result = await tool.execute(call)
    assert not result.is_error


@pytest.mark.asyncio
async def test_jpeg_magic_bytes_accepted(tool, monkeypatch):
    transport = _make_mock_transport(image_bytes=_JPEG_MAGIC)
    monkeypatch.setattr(
        "opencomputer.tools.vision_analyze._make_async_client",
        lambda timeout=60.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c7",
        name="VisionAnalyze",
        arguments={"image_url": "https://example.com/photo.jpg"},
    )
    result = await tool.execute(call)
    assert not result.is_error


@pytest.mark.asyncio
async def test_no_api_key_returns_clear_error():
    tool = VisionAnalyzeTool(api_key=None)
    with patch.dict("os.environ", {}, clear=False):
        # Make sure env var also unset
        import os
        os.environ.pop("ANTHROPIC_API_KEY", None)
        b64 = base64.b64encode(_PNG_MAGIC).decode()
        call = ToolCall(
            id="c8",
            name="VisionAnalyze",
            arguments={"image_base64": b64},
        )
        result = await tool.execute(call)
        assert result.is_error
        assert "api key" in result.content.lower() or "ANTHROPIC_API_KEY" in result.content


def test_schema_shape(tool):
    s = tool.schema
    assert s.name == "VisionAnalyze"
    props = s.parameters["properties"]
    assert "image_url" in props
    assert "image_base64" in props
    assert "prompt" in props
    # Neither is "required" individually since it's an OR


@pytest.mark.asyncio
async def test_oversized_image_rejected(tool, monkeypatch):
    """Images over a hard size limit are rejected to avoid cost blowup."""
    huge = _PNG_MAGIC + b"x" * (10 * 1024 * 1024 + 100)  # > 10MB
    transport = _make_mock_transport(image_bytes=huge)
    monkeypatch.setattr(
        "opencomputer.tools.vision_analyze._make_async_client",
        lambda timeout=60.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c9",
        name="VisionAnalyze",
        arguments={"image_url": "https://example.com/huge.png"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "too large" in result.content.lower() or "exceeds" in result.content.lower()
