"""VisionAnalyze.image_path — read images from disk instead of from context.

Closes the bootstrap deadlock the user hit: previously, the only way to
analyze a screenshot was ``image_url`` (SSRF-blocked for localhost) or
``image_base64`` (which still has to be in the agent's context to pass it
as a tool argument — circular). Now ``image_path`` lets the agent point
VisionAnalyze at the file the screenshot tool wrote, and the data flows
disk → vision API → text response, never through the agent's context.

Path safety: ONLY paths under ``<profile_home>/tool_result_storage/``
are accepted. Anything else (``/etc/passwd``, ``~/.ssh/id_rsa``, the
user's home dir) is rejected. ``Path.resolve()`` collapses symlink
traversal before the prefix check so ``<storage>/../etc/shadow`` also
gets rejected.
"""
from __future__ import annotations

import pytest


def _make_safe_image(tmp_path, monkeypatch):
    """Write a tiny valid PNG inside the safe directory and return its path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    storage = tmp_path / "tool_result_storage" / "screenshots"
    storage.mkdir(parents=True)
    path = storage / "oc-screen-001.png"
    # Minimal valid PNG (1x1, transparent)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63f8cfc0c0c0000000050001fed8a4250000000049454e44ae426082"
    )
    path.write_bytes(png_bytes)
    return path, png_bytes


def test_is_safe_image_path_accepts_storage_subpath(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    safe = tmp_path / "tool_result_storage" / "screenshots" / "shot.png"
    safe.parent.mkdir(parents=True)
    safe.write_bytes(b"\x89PNG\r\n\x1a\n")

    from opencomputer.tools.vision_analyze import _is_safe_image_path
    assert _is_safe_image_path(safe) is True


def test_is_safe_image_path_rejects_outside_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    from opencomputer.tools.vision_analyze import _is_safe_image_path
    assert _is_safe_image_path(outside) is False


def test_is_safe_image_path_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    """``<storage>/../etc/passwd`` must not slip through. Path.resolve()
    collapses .. before the prefix check."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    storage = tmp_path / "tool_result_storage" / "screenshots"
    storage.mkdir(parents=True)
    traversal = storage / ".." / ".." / ".." / "etc" / "passwd"

    from opencomputer.tools.vision_analyze import _is_safe_image_path
    assert _is_safe_image_path(traversal) is False


@pytest.mark.asyncio
async def test_image_path_round_trips_through_anthropic_call(tmp_path, monkeypatch) -> None:
    """Happy path: an image at a safe path round-trips into the Anthropic
    request body as a base64 image block, and the tool returns the text
    response. The Anthropic call itself is stubbed."""
    path, png_bytes = _make_safe_image(tmp_path, monkeypatch)

    from opencomputer.tools.vision_analyze import VisionAnalyzeTool
    from plugin_sdk.core import ToolCall

    tool = VisionAnalyzeTool(api_key="sk-ant-test")

    # Capture what we send to the API
    captured: dict[str, object] = {}
    async def _fake_call(self_, image_b64, mime, prompt, api_key):
        captured["b64"] = image_b64
        captured["mime"] = mime
        captured["prompt"] = prompt
        return "A clean desktop with a terminal window."

    monkeypatch.setattr(VisionAnalyzeTool, "_call_anthropic", _fake_call)

    result = await tool.execute(ToolCall(
        id="t1", name="VisionAnalyze",
        arguments={"image_path": str(path), "prompt": "describe"},
    ))

    assert not result.is_error
    assert result.content == "A clean desktop with a terminal window."
    import base64
    assert base64.b64decode(captured["b64"]) == png_bytes
    assert captured["mime"] == "image/png"
    assert captured["prompt"] == "describe"


@pytest.mark.asyncio
async def test_image_path_outside_safe_set_returns_error(tmp_path, monkeypatch) -> None:
    """A path outside <profile_home>/tool_result_storage/ is rejected with
    a clear error message — the tool does NOT read the file."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    unsafe = tmp_path / "secret.png"
    unsafe.write_bytes(b"\x89PNG\r\n\x1a\nBADBAD")

    from opencomputer.tools.vision_analyze import VisionAnalyzeTool
    from plugin_sdk.core import ToolCall

    tool = VisionAnalyzeTool(api_key="sk-ant-test")
    result = await tool.execute(ToolCall(
        id="t1", name="VisionAnalyze",
        arguments={"image_path": str(unsafe)},
    ))

    assert result.is_error
    assert "outside the safe set" in result.content


@pytest.mark.asyncio
async def test_multiple_image_sources_rejected(tmp_path, monkeypatch) -> None:
    """Exactly one of image_url/image_path/image_base64 may be set."""
    path, _ = _make_safe_image(tmp_path, monkeypatch)

    from opencomputer.tools.vision_analyze import VisionAnalyzeTool
    from plugin_sdk.core import ToolCall

    tool = VisionAnalyzeTool(api_key="sk-ant-test")
    result = await tool.execute(ToolCall(
        id="t1", name="VisionAnalyze",
        arguments={
            "image_path": str(path),
            "image_base64": "AAAA",
        },
    ))
    assert result.is_error
    assert "exactly ONE" in result.content


@pytest.mark.asyncio
async def test_no_image_source_rejected() -> None:
    from opencomputer.tools.vision_analyze import VisionAnalyzeTool
    from plugin_sdk.core import ToolCall

    tool = VisionAnalyzeTool(api_key="sk-ant-test")
    result = await tool.execute(ToolCall(
        id="t1", name="VisionAnalyze",
        arguments={},
    ))
    assert result.is_error
    assert "must provide" in result.content


@pytest.mark.asyncio
async def test_image_path_non_image_content_rejected(tmp_path, monkeypatch) -> None:
    """Magic-byte sniff catches text/HTML masquerading as a PNG."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    storage = tmp_path / "tool_result_storage" / "screenshots"
    storage.mkdir(parents=True)
    fake = storage / "fake.png"
    fake.write_bytes(b"<html>i am not an image</html>")

    from opencomputer.tools.vision_analyze import VisionAnalyzeTool
    from plugin_sdk.core import ToolCall

    tool = VisionAnalyzeTool(api_key="sk-ant-test")
    result = await tool.execute(ToolCall(
        id="t1", name="VisionAnalyze",
        arguments={"image_path": str(fake)},
    ))
    assert result.is_error
    assert "magic-byte sniff failed" in result.content
