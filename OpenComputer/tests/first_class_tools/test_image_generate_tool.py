"""Tests for ImageGenerateTool — first-class image generation via FAL."""
import json

import httpx
import pytest

from opencomputer.tools.image_generate import ImageGenerateTool
from plugin_sdk.core import ToolCall


def _mock_fal_response(image_url: str = "https://fal.media/files/abc/cat.png") -> dict:
    return {
        "images": [{"url": image_url, "width": 1024, "height": 1024}],
        "seed": 12345,
        "timings": {"inference": 1.2},
    }


def _make_mock_transport(response: dict | None = None, status: int = 200) -> httpx.MockTransport:
    payload = response if response is not None else _mock_fal_response()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


@pytest.fixture
def tool():
    return ImageGenerateTool(api_key="test-key")


@pytest.mark.asyncio
async def test_generate_with_default_model(tool, monkeypatch):
    transport = _make_mock_transport()
    monkeypatch.setattr(
        "opencomputer.tools.image_generate._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c1",
        name="ImageGenerate",
        arguments={"prompt": "a cat sitting on a sofa"},
    )
    result = await tool.execute(call)
    assert not result.is_error
    assert "fal.media" in result.content or "images" in result.content.lower()


@pytest.mark.asyncio
async def test_generate_with_explicit_model(tool, monkeypatch):
    transport = _make_mock_transport()
    monkeypatch.setattr(
        "opencomputer.tools.image_generate._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c2",
        name="ImageGenerate",
        arguments={
            "prompt": "a watercolor sunset",
            "model": "fal-ai/flux/schnell",
        },
    )
    result = await tool.execute(call)
    assert not result.is_error


@pytest.mark.asyncio
async def test_missing_prompt_returns_error(tool):
    call = ToolCall(
        id="c3",
        name="ImageGenerate",
        arguments={},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "prompt" in result.content.lower()


@pytest.mark.asyncio
async def test_no_api_key_returns_clear_error(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    tool = ImageGenerateTool(api_key=None)
    call = ToolCall(
        id="c4",
        name="ImageGenerate",
        arguments={"prompt": "x"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "FAL_KEY" in result.content or "api key" in result.content.lower()


@pytest.mark.asyncio
async def test_extra_payload_overrides_passed_through(tool, monkeypatch):
    """Optional `payload` dict is merged into the FAL request body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_mock_fal_response())

    monkeypatch.setattr(
        "opencomputer.tools.image_generate._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout),
    )
    call = ToolCall(
        id="c5",
        name="ImageGenerate",
        arguments={
            "prompt": "x",
            "payload": {"image_size": "square_hd", "num_images": 2},
        },
    )
    await tool.execute(call)
    assert captured["body"]["prompt"] == "x"
    assert captured["body"]["image_size"] == "square_hd"
    assert captured["body"]["num_images"] == 2


@pytest.mark.asyncio
async def test_api_failure_surfaces_error(tool, monkeypatch):
    transport = _make_mock_transport(response={"detail": "rate limited"}, status=429)
    monkeypatch.setattr(
        "opencomputer.tools.image_generate._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c6",
        name="ImageGenerate",
        arguments={"prompt": "x"},
    )
    result = await tool.execute(call)
    assert result.is_error
    assert "fal" in result.content.lower() or "429" in result.content


def test_schema_shape(tool):
    s = tool.schema
    assert s.name == "ImageGenerate"
    props = s.parameters["properties"]
    assert "prompt" in props
    assert "model" in props
    assert "payload" in props
    required = s.parameters.get("required", [])
    assert "prompt" in required


@pytest.mark.asyncio
async def test_image_url_extracted_from_response(tool, monkeypatch):
    """Output should prominently feature the image URL."""
    expected_url = "https://fal.media/files/abc/specific-cat.png"
    transport = _make_mock_transport(response=_mock_fal_response(image_url=expected_url))
    monkeypatch.setattr(
        "opencomputer.tools.image_generate._make_async_client",
        lambda timeout=120.0: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    call = ToolCall(
        id="c7",
        name="ImageGenerate",
        arguments={"prompt": "a cat"},
    )
    result = await tool.execute(call)
    assert expected_url in result.content
