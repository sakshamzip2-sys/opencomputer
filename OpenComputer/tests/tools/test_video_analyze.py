"""Tests for opencomputer.tools.video_analyze (Wave 5 T7)."""

from __future__ import annotations

import pytest

from opencomputer.tools.video_analyze import (
    MAX_VIDEO_BYTES,
    MIN_TIMEOUT_S,
    SUPPORTED_VIDEO_FORMATS,
    WARN_VIDEO_BYTES,
    VideoAnalyzeTool,
    video_analyze,
)
from plugin_sdk.core import ToolCall


def test_supported_formats_includes_canonical_six():
    for ext in ("mp4", "webm", "mov", "avi", "mkv", "mpeg"):
        assert ext in SUPPORTED_VIDEO_FORMATS


def test_max_size_50mb():
    assert MAX_VIDEO_BYTES == 50 * 1024 * 1024


def test_warn_size_20mb():
    assert WARN_VIDEO_BYTES == 20 * 1024 * 1024


def test_min_timeout_180s():
    assert MIN_TIMEOUT_S == 180.0


@pytest.mark.asyncio
async def test_rejects_unsupported_format(tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"fakegif")
    with pytest.raises(ValueError, match="Unsupported"):
        await video_analyze(path=str(p), prompt="describe")


@pytest.mark.asyncio
async def test_rejects_oversize(tmp_path):
    p = tmp_path / "big.mp4"
    p.write_bytes(b"\x00" * (MAX_VIDEO_BYTES + 1))
    with pytest.raises(ValueError, match="50"):
        await video_analyze(path=str(p), prompt="describe")


@pytest.mark.asyncio
async def test_happy_path_calls_complete_video(tmp_path, monkeypatch):
    p = tmp_path / "test.mp4"
    p.write_bytes(b"\x00" * 1024)  # 1 KiB

    captured: dict = {}

    async def fake_complete_video(
        *,
        video_base64: str,
        mime_type: str,
        prompt: str,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        captured["mime_type"] = mime_type
        captured["prompt"] = prompt
        captured["model"] = model
        # b64 should be non-empty
        assert video_base64
        return "a video of nothing"

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_video",
        fake_complete_video,
    )
    result = await video_analyze(
        path=str(p), prompt="What's happening?", model="aux-x",
    )
    assert "video of nothing" in result
    assert captured["mime_type"] == "video/mp4"
    assert captured["prompt"] == "What's happening?"
    assert captured["model"] == "aux-x"


@pytest.mark.asyncio
async def test_happy_path_default_prompt(tmp_path, monkeypatch):
    p = tmp_path / "test.webm"
    p.write_bytes(b"\x00" * 100)

    captured = {}

    async def fake_complete_video(*, prompt, **kw):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_video",
        fake_complete_video,
    )
    await video_analyze(path=str(p), prompt="")
    assert captured["prompt"] == "Describe this video in detail."


@pytest.mark.asyncio
async def test_video_analyze_tool_execute_happy_path(tmp_path, monkeypatch):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"\x00" * 100)

    async def fake_complete_video(*a, **kw):
        return "tool-text"

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_video",
        fake_complete_video,
    )
    tool = VideoAnalyzeTool()
    result = await tool.execute(
        ToolCall(id="c1", name="VideoAnalyze", arguments={
            "path": str(p), "prompt": "describe",
        }),
    )
    assert result.is_error is False
    assert "tool-text" in result.content


@pytest.mark.asyncio
async def test_video_analyze_tool_missing_path():
    tool = VideoAnalyzeTool()
    result = await tool.execute(
        ToolCall(id="c2", name="VideoAnalyze", arguments={"prompt": "x"}),
    )
    assert result.is_error is True
    assert "path" in result.content


@pytest.mark.asyncio
async def test_video_analyze_tool_unsupported_format(tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"fake")
    tool = VideoAnalyzeTool()
    result = await tool.execute(
        ToolCall(id="c3", name="VideoAnalyze", arguments={
            "path": str(p), "prompt": "describe",
        }),
    )
    assert result.is_error is True
    assert "Unsupported" in result.content


@pytest.mark.asyncio
async def test_video_analyze_tool_provider_error(tmp_path, monkeypatch):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"\x00")

    async def fake_complete_video(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_video",
        fake_complete_video,
    )
    tool = VideoAnalyzeTool()
    result = await tool.execute(
        ToolCall(id="c4", name="VideoAnalyze", arguments={
            "path": str(p), "prompt": "describe",
        }),
    )
    assert result.is_error is True
    assert "provider call failed" in result.content
    assert "provider down" in result.content


def test_tool_schema_lists_required_path_and_prompt():
    tool = VideoAnalyzeTool()
    schema = tool.schema
    assert schema.name == "VideoAnalyze"
    assert "path" in schema.parameters["properties"]
    assert "prompt" in schema.parameters["properties"]
    assert set(schema.parameters["required"]) == {"path", "prompt"}
