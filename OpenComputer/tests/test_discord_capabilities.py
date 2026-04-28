"""Tests for G.12 / Tier 2.8 — Discord adapter reactions + edit + delete.

Mocks discord.py via simple substitutes so tests don't need a live bot
or network. Verifies the capability flag + that each new method calls
the right discord.py path with the right args.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk import ChannelCapabilities
from plugin_sdk.core import SendResult


def _load_discord_adapter():
    spec = importlib.util.spec_from_file_location(
        "discord_adapter_test_g12",
        Path(__file__).resolve().parent.parent / "extensions" / "discord" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DiscordAdapter, mod


@pytest.fixture
def adapter_with_mocks():
    """Construct a DiscordAdapter with discord.py client mocked.

    Returns ``(adapter, mock_message)`` where mock_message is the message
    object that fetch_message() returns. Tests assert against its calls.
    """
    DiscordAdapter, _ = _load_discord_adapter()

    # Mock the discord.Client so __init__ doesn't open a connection.
    a = object.__new__(DiscordAdapter)  # bypass __init__
    a.config = {}
    a.token = "fake"
    a._client = MagicMock()
    a._bot_user_id = 12345
    a._client_task = None
    a._channel_cache = {}
    a._ready_event = MagicMock()

    # Mock message has the discord.Message methods we exercise
    mock_message = MagicMock()
    mock_message.id = 555
    mock_message.add_reaction = AsyncMock()
    mock_message.edit = AsyncMock()
    mock_message.delete = AsyncMock()

    # Mock channel returns mock_message from fetch_message
    mock_channel = MagicMock()
    mock_channel.fetch_message = AsyncMock(return_value=mock_message)
    mock_channel.id = 999

    # Pre-cache the channel so _resolve_channel returns it without hitting fetch_channel
    a._channel_cache["999"] = mock_channel

    return a, mock_message, mock_channel


class TestCapabilityFlag:
    def test_advertises_full_g12_set(self) -> None:
        DiscordAdapter, _ = _load_discord_adapter()
        c = DiscordAdapter.capabilities
        for cap in (
            ChannelCapabilities.TYPING,
            ChannelCapabilities.REACTIONS,
            ChannelCapabilities.EDIT_MESSAGE,
            ChannelCapabilities.DELETE_MESSAGE,
            ChannelCapabilities.THREADS,
        ):
            assert c & cap, f"{cap.name} should be set"

    def test_does_not_advertise_unimplemented(self) -> None:
        """Discord capabilities G.12 ships shouldn't include voice/photo/document."""
        DiscordAdapter, _ = _load_discord_adapter()
        c = DiscordAdapter.capabilities
        for cap in (
            ChannelCapabilities.VOICE_OUT,
            ChannelCapabilities.VOICE_IN,
            ChannelCapabilities.PHOTO_OUT,
            ChannelCapabilities.PHOTO_IN,
            ChannelCapabilities.DOCUMENT_OUT,
            ChannelCapabilities.DOCUMENT_IN,
        ):
            assert not (c & cap), f"{cap.name} shouldn't be set yet"


class TestReactions:
    @pytest.mark.asyncio
    async def test_add_reaction_calls_discord(self, adapter_with_mocks) -> None:
        adapter, msg, channel = adapter_with_mocks
        result = await adapter.send_reaction("999", "555", "👍")
        assert result.success
        msg.add_reaction.assert_awaited_once_with("👍")

    @pytest.mark.asyncio
    async def test_unknown_message_returns_error(self, adapter_with_mocks) -> None:
        import discord

        adapter, _, channel = adapter_with_mocks
        channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "missing")
        result = await adapter.send_reaction("999", "12345", "👍")
        assert not result.success
        assert "not found" in result.error.lower()


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edit_calls_discord(self, adapter_with_mocks) -> None:
        adapter, msg, _ = adapter_with_mocks
        result = await adapter.edit_message("999", "555", "updated text")
        assert result.success
        # PR 4.3 — edit_message also passes allowed_mentions; assert
        # on content only (mention-policy is covered in the
        # test_discord_allowed_mentions module).
        msg.edit.assert_awaited_once()
        assert msg.edit.await_args.kwargs["content"] == "updated text"

    @pytest.mark.asyncio
    async def test_edit_truncates_to_max_length(self, adapter_with_mocks) -> None:
        adapter, msg, _ = adapter_with_mocks
        long_text = "x" * 5000
        await adapter.edit_message("999", "555", long_text)
        kwargs = msg.edit.await_args.kwargs
        assert len(kwargs["content"]) == 2000  # max_message_length

    @pytest.mark.asyncio
    async def test_forbidden_returns_friendly_error(self, adapter_with_mocks) -> None:
        import discord

        adapter, msg, _ = adapter_with_mocks
        msg.edit.side_effect = discord.Forbidden(MagicMock(), "no perm")
        result = await adapter.edit_message("999", "555", "x")
        assert not result.success
        assert "forbidden" in result.error.lower()


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_delete_calls_discord(self, adapter_with_mocks) -> None:
        adapter, msg, _ = adapter_with_mocks
        result = await adapter.delete_message("999", "555")
        assert result.success
        msg.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_message_returns_error(self, adapter_with_mocks) -> None:
        import discord

        adapter, _, channel = adapter_with_mocks
        channel.fetch_message.side_effect = discord.NotFound(MagicMock(), "missing")
        result = await adapter.delete_message("999", "12345")
        assert not result.success


class TestChannelResolution:
    @pytest.mark.asyncio
    async def test_uncached_channel_falls_through_to_fetch(self) -> None:
        """When a channel isn't cached, _resolve_channel should call fetch_channel."""
        DiscordAdapter, _ = _load_discord_adapter()
        a = object.__new__(DiscordAdapter)
        a._client = MagicMock()
        a._channel_cache = {}

        mock_channel = MagicMock()
        a._client.fetch_channel = AsyncMock(return_value=mock_channel)

        result = await a._resolve_channel("777")
        assert result is mock_channel
        a._client.fetch_channel.assert_awaited_once_with(777)
        # Also caches for next time
        assert a._channel_cache["777"] is mock_channel

    @pytest.mark.asyncio
    async def test_unknown_channel_returns_none(self) -> None:
        DiscordAdapter, _ = _load_discord_adapter()
        a = object.__new__(DiscordAdapter)
        a._client = MagicMock()
        a._channel_cache = {}
        a._client.fetch_channel = AsyncMock(side_effect=Exception("404"))

        result = await a._resolve_channel("nope")
        assert result is None
