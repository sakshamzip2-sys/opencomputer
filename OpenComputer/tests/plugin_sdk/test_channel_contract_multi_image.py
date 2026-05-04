"""Tests for BaseChannelAdapter.send_multiple_images default loop (Wave 5 T10)."""

from __future__ import annotations

import pytest

from plugin_sdk.channel_contract import (
    BaseChannelAdapter,
    ChannelCapabilities,
    SendResult,
)
from plugin_sdk.core import Platform


class _FakeAdapter(BaseChannelAdapter):
    """Minimal concrete adapter that records every send_photo call."""

    platform = Platform.WEBHOOK  # any valid platform; webhook is the simplest
    capabilities = ChannelCapabilities.PHOTO_OUT

    def __init__(self) -> None:
        super().__init__(config={})
        self.sent_singles: list[tuple[str, str, str]] = []

    async def connect(self) -> None:  # noqa: D401
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id: str, text: str, **kwargs):
        return SendResult(success=True, message_id="x")

    async def send_photo(
        self, chat_id, photo_path, caption: str = "", **kwargs,
    ) -> SendResult:
        self.sent_singles.append((chat_id, str(photo_path), caption))
        return SendResult(success=True, message_id="x")


@pytest.mark.asyncio
async def test_send_multiple_images_default_loops_send_photo():
    a = _FakeAdapter()
    await a.send_multiple_images(
        "chat:1",
        ["/a.png", "/b.png", "/c.png"],
        caption="batch",
    )
    assert len(a.sent_singles) == 3
    # First image gets the caption; subsequent images get ""
    assert a.sent_singles[0][2] == "batch"
    assert a.sent_singles[1][2] == ""
    assert a.sent_singles[2][2] == ""
    # All three target the same chat
    for chat_id, _, _ in a.sent_singles:
        assert chat_id == "chat:1"


@pytest.mark.asyncio
async def test_send_multiple_images_empty_is_noop():
    a = _FakeAdapter()
    await a.send_multiple_images("chat:1", [])
    assert a.sent_singles == []


@pytest.mark.asyncio
async def test_send_multiple_images_path_objects():
    """list[Path] is acceptable too."""
    from pathlib import Path

    a = _FakeAdapter()
    await a.send_multiple_images(
        "chat:1",
        [Path("/p.png"), Path("/q.png")],
        caption="cap",
    )
    assert len(a.sent_singles) == 2
    assert a.sent_singles[0][1] == "/p.png"


@pytest.mark.asyncio
async def test_send_multiple_images_falls_back_to_send_image_when_photo_unavailable():
    """If send_photo raises NotImplementedError, default tries send_image."""

    class _UrlOnlyAdapter(BaseChannelAdapter):
        platform = Platform.WEBHOOK
        capabilities = ChannelCapabilities.NONE

        def __init__(self) -> None:
            super().__init__(config={})
            self.sent_via_url: list[tuple[str, str, str]] = []

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, text, **kwargs):
            return SendResult(success=True, message_id="x")

        async def send_image(self, chat_id, image_url, caption: str = ""):
            self.sent_via_url.append((chat_id, image_url, caption))
            return SendResult(success=True, message_id="x")

    a = _UrlOnlyAdapter()
    await a.send_multiple_images("c:1", ["/a.png", "/b.png"], caption="c")
    assert len(a.sent_via_url) == 2
    assert a.sent_via_url[0][2] == "c"
    assert a.sent_via_url[1][2] == ""
