"""Tests for DiscordAdapter.send_multiple_images native multi-file send.

Wave 5 T11 closure — Hermes-port (3de8e2168). Discord's REST allows up to
10 attachments per channel.send() call; this override chunks at 10 and
falls back to the base loop on platform errors.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_adapter():
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "discord"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_discord_adapter_for_T11", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def discord_adapter_class():
    return _load_adapter().DiscordAdapter


def _make_stub_adapter(cls):
    """Construct DiscordAdapter without running its real __init__."""
    a = cls.__new__(cls)
    a._channel_cache = {}
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock(return_value=MagicMock(id=42))
    a._channel_cache["c1"] = fake_channel
    a._client = MagicMock()
    a._build_allowed_mentions = lambda: None
    return a, fake_channel


@pytest.mark.asyncio
async def test_empty_list_is_noop(discord_adapter_class):
    a, ch = _make_stub_adapter(discord_adapter_class)
    await a.send_multiple_images("c1", [])
    ch.send.assert_not_called()


@pytest.mark.asyncio
async def test_under_10_single_send_call(tmp_path, discord_adapter_class):
    a, ch = _make_stub_adapter(discord_adapter_class)
    paths = []
    for i in range(5):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    await a.send_multiple_images("c1", paths, caption="hi")
    assert ch.send.await_count == 1
    call = ch.send.await_args
    assert call.kwargs.get("content") == "hi"
    files = call.kwargs.get("files") or []
    assert len(files) == 5


@pytest.mark.asyncio
async def test_over_10_chunked_into_two_sends(tmp_path, discord_adapter_class):
    a, ch = _make_stub_adapter(discord_adapter_class)
    paths = []
    for i in range(15):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG")
        paths.append(str(p))
    await a.send_multiple_images("c1", paths, caption="hi")
    assert ch.send.await_count == 2
    # First chunk carries the caption, second has content=None
    first_call = ch.send.await_args_list[0]
    second_call = ch.send.await_args_list[1]
    assert first_call.kwargs.get("content") == "hi"
    assert second_call.kwargs.get("content") is None


@pytest.mark.asyncio
async def test_no_caption_means_content_none_on_first_chunk(tmp_path, discord_adapter_class):
    a, ch = _make_stub_adapter(discord_adapter_class)
    a_p = tmp_path / "a.png"
    b_p = tmp_path / "b.png"
    a_p.write_bytes(b"a")
    b_p.write_bytes(b"b")
    await a.send_multiple_images("c1", [str(a_p), str(b_p)], caption="")
    assert ch.send.await_count == 1
    assert ch.send.await_args.kwargs.get("content") is None


@pytest.mark.asyncio
async def test_falls_back_to_base_loop_on_send_error(tmp_path, discord_adapter_class):
    a, ch = _make_stub_adapter(discord_adapter_class)
    ch.send.side_effect = RuntimeError("rate limited")
    a.send_image = AsyncMock(return_value=MagicMock(success=True))
    a_p = tmp_path / "a.png"
    b_p = tmp_path / "b.png"
    a_p.write_bytes(b"a")
    b_p.write_bytes(b"b")
    await a.send_multiple_images("c1", [str(a_p), str(b_p)], caption="x")
    # Native attempt happened (raised), then base loop ran send_image twice
    assert ch.send.await_count == 1
    assert a.send_image.await_count == 2


@pytest.mark.asyncio
async def test_unknown_channel_is_graceful_noop(discord_adapter_class):
    a, _ = _make_stub_adapter(discord_adapter_class)
    a._channel_cache = {}  # cache empty
    a._client.fetch_channel = AsyncMock(side_effect=ValueError("404"))
    await a.send_multiple_images("nonexistent", ["/a.png"])
    # No exception; just a logged warning + return
