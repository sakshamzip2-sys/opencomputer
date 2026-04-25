"""Tests for ChannelCapabilities flag enum + BaseChannelAdapter defaults.

Sub-project G refactor R1 — adapters declare their capabilities so callers
can gracefully degrade. Default optional methods raise NotImplementedError;
``send_typing`` is the one no-op exception (back-compat with adapters that
pre-date the capability flag).
"""

from __future__ import annotations

from typing import Any

import pytest

from plugin_sdk import ChannelCapabilities
from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform, SendResult


class _MinimalAdapter(BaseChannelAdapter):
    """Only required methods; capabilities = NONE by default."""

    platform = Platform.TELEGRAM

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        return SendResult(success=True)


class TestChannelCapabilities:
    def test_default_is_none(self) -> None:
        adapter = _MinimalAdapter({})
        assert adapter.capabilities == ChannelCapabilities.NONE

    def test_flag_combination(self) -> None:
        combo = ChannelCapabilities.TYPING | ChannelCapabilities.REACTIONS
        assert ChannelCapabilities.TYPING in combo
        assert ChannelCapabilities.REACTIONS in combo
        assert ChannelCapabilities.VOICE_OUT not in combo

    def test_all_capabilities_distinct(self) -> None:
        """Each named cap should be a distinct bit (no accidental aliasing)."""
        names = [n for n in ChannelCapabilities.__members__ if n != "NONE"]
        values = [ChannelCapabilities[n].value for n in names]
        assert len(values) == len(set(values)), "duplicate capability bits"

    def test_supports_check_idiom(self) -> None:
        """Idiom: `if adapter.capabilities & X: do_x()`."""
        adapter = _MinimalAdapter({})
        if adapter.capabilities & ChannelCapabilities.PHOTO_OUT:
            pytest.fail("MinimalAdapter shouldn't advertise PHOTO_OUT")


class TestBaseAdapterDefaults:
    """Default optional methods raise NotImplementedError except send_typing."""

    @pytest.fixture
    def adapter(self) -> _MinimalAdapter:
        return _MinimalAdapter({})

    @pytest.mark.asyncio
    async def test_send_typing_default_noop(self, adapter: _MinimalAdapter) -> None:
        # Returns None without raising
        assert await adapter.send_typing("chat") is None

    @pytest.mark.asyncio
    async def test_send_image_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.send_image("chat", "https://example.com/img.png")

    @pytest.mark.asyncio
    async def test_send_photo_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.send_photo("chat", "/tmp/foo.jpg")

    @pytest.mark.asyncio
    async def test_send_document_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.send_document("chat", "/tmp/foo.pdf")

    @pytest.mark.asyncio
    async def test_send_voice_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.send_voice("chat", "/tmp/foo.ogg")

    @pytest.mark.asyncio
    async def test_send_reaction_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.send_reaction("chat", "msg-id", "👍")

    @pytest.mark.asyncio
    async def test_edit_message_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.edit_message("chat", "msg-id", "new text")

    @pytest.mark.asyncio
    async def test_delete_message_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.delete_message("chat", "msg-id")

    @pytest.mark.asyncio
    async def test_download_attachment_default_raises(self, adapter: _MinimalAdapter) -> None:
        with pytest.raises(NotImplementedError):
            await adapter.download_attachment(file_id="abc", dest_dir="/tmp")

    @pytest.mark.asyncio
    async def test_send_notification_default_proxies_send(self, adapter: _MinimalAdapter) -> None:
        # Default falls back to send() — succeeds
        result = await adapter.send_notification("chat", "alert")
        assert result.success
