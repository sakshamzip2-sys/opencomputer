"""Tests for TelegramAdapter.send_multiple_images native sendMediaGroup override.

Wave 5 T11 — Hermes-port (3de8e2168). Mirrors hermes-agent's per-platform
override: chunks static photos into 10-album batches via sendMediaGroup,
peels off animated formats (.gif/.webm) which Telegram doesn't allow in
albums and routes them through sendAnimation, and falls back to the base
per-image loop on any platform-side failure.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_adapter():
    """Import telegram adapter via spec_from_file_location to avoid
    sys.modules collisions with other plugins (CLAUDE.md gotcha #1)."""
    path = (
        Path(__file__).resolve().parents[2]
        / "extensions"
        / "telegram"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_telegram_adapter_for_T11", str(path)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def telegram_adapter_class():
    return _load_adapter().TelegramAdapter


def _make_stub_adapter(cls):
    """Construct a TelegramAdapter without running its real __init__.

    Sets just the fields send_multiple_images / _send_media_group touch.
    """
    a = cls.__new__(cls)
    a._client = MagicMock()
    a.base_url = "https://api.telegram.org/botFAKE"
    a._post_with_retry = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"ok": True}),
    )
    a._send_media = AsyncMock(return_value=MagicMock(success=True))
    return a


@pytest.mark.asyncio
async def test_empty_list_is_noop(telegram_adapter_class):
    a = _make_stub_adapter(telegram_adapter_class)
    await a.send_multiple_images("chat:1", [])
    a._post_with_retry.assert_not_called()
    a._send_media.assert_not_called()


@pytest.mark.asyncio
async def test_static_chunks_into_albums_of_10(tmp_path, telegram_adapter_class):
    a = _make_stub_adapter(telegram_adapter_class)
    paths: list[str | Path] = []
    for i in range(15):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.append(p)
    await a.send_multiple_images("chat:1", paths, caption="hi")
    # 15 photos → 2 sendMediaGroup calls (10 + 5)
    assert a._post_with_retry.await_count == 2


@pytest.mark.asyncio
async def test_first_album_gets_caption_only(tmp_path, telegram_adapter_class):
    a = _make_stub_adapter(telegram_adapter_class)
    paths: list[Path] = []
    for i in range(12):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.append(p)
    await a.send_multiple_images("chat:1", paths, caption="album-caption")
    # First call carries the caption, second doesn't.
    first_call_data = a._post_with_retry.await_args_list[0].kwargs["data"]
    assert "album-caption" in first_call_data["media"]
    second_call_data = a._post_with_retry.await_args_list[1].kwargs["data"]
    assert "album-caption" not in second_call_data["media"]


@pytest.mark.asyncio
async def test_animations_peeled_to_send_animation(tmp_path, telegram_adapter_class):
    a = _make_stub_adapter(telegram_adapter_class)
    statics = [tmp_path / "a.png", tmp_path / "b.png"]
    for s in statics:
        s.write_bytes(b"\x89PNG\r\n\x1a\n")
    gif = tmp_path / "x.gif"
    gif.write_bytes(b"GIF89a")
    paths: list[str | Path] = [statics[0], gif, statics[1]]
    await a.send_multiple_images("chat:1", paths, caption="cap")
    # 2 statics → 1 sendMediaGroup call
    assert a._post_with_retry.await_count == 1
    # 1 animation → 1 sendAnimation call via _send_media
    assert a._send_media.await_count == 1
    # The sendAnimation call shouldn't carry the caption (album already did)
    anim_call = a._send_media.await_args_list[0]
    # _send_media positional: (chat_id, path, endpoint, field, caption, ...)
    assert anim_call.args[2] == "sendAnimation"
    assert anim_call.args[4] == ""  # caption


@pytest.mark.asyncio
async def test_animations_only_keep_caption_on_first(tmp_path, telegram_adapter_class):
    """When there are NO statics, the first animation should carry the caption."""
    a = _make_stub_adapter(telegram_adapter_class)
    g1 = tmp_path / "1.gif"
    g2 = tmp_path / "2.gif"
    g1.write_bytes(b"GIF")
    g2.write_bytes(b"GIF")
    await a.send_multiple_images("chat:1", [g1, g2], caption="hi")
    # No sendMediaGroup; two sendAnimation calls
    assert a._post_with_retry.await_count == 0
    assert a._send_media.await_count == 2
    # First sendAnimation carries the caption
    assert a._send_media.await_args_list[0].args[4] == "hi"
    # Second carries empty
    assert a._send_media.await_args_list[1].args[4] == ""


@pytest.mark.asyncio
async def test_falls_back_to_base_loop_on_telegram_error(
    tmp_path, telegram_adapter_class,
):
    """If sendMediaGroup returns non-OK, fall back to per-image send_photo loop."""
    a = _make_stub_adapter(telegram_adapter_class)
    a._post_with_retry = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: {"ok": False}),
    )
    a.send_photo = AsyncMock(return_value=MagicMock(success=True))
    paths: list[Path] = []
    for i in range(3):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.append(p)
    await a.send_multiple_images("chat:1", paths, caption="cap")
    # Native attempt happened
    assert a._post_with_retry.await_count == 1
    # Fallback ran send_photo for each
    assert a.send_photo.await_count == 3


@pytest.mark.asyncio
async def test_oversize_photo_triggers_fallback(
    tmp_path, telegram_adapter_class,
):
    a = _make_stub_adapter(telegram_adapter_class)
    a.send_photo = AsyncMock(return_value=MagicMock(success=True))
    big = tmp_path / "big.png"
    big.write_bytes(b"\x00" * (12 * 1024 * 1024))  # > 10 MB telegram cap
    await a.send_multiple_images("chat:1", [big], caption="x")
    # Native validation rejects oversize → falls back to per-image loop
    assert a.send_photo.await_count == 1
