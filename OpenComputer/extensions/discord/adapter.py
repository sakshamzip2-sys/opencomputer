"""
DiscordAdapter — Discord channel adapter using discord.py.

Uses discord.py's event-driven client: on_message → MessageEvent →
gateway dispatch. Sends replies via message.channel.send. Handles the
2000-char message limit with split-on-line-boundary chunking.

Connects via DISCORD_BOT_TOKEN env var. Requires the "message_content"
intent — enable it in the bot's settings on Discord Developer Portal.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord

from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.discord")


def _chunk_2000(text: str, limit: int = 2000) -> list[str]:
    """Split `text` so each chunk is <= limit chars, respecting line breaks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        ll = len(line)
        # Single line > limit: hard-split
        if ll > limit:
            if current:
                chunks.append("".join(current))
                current, current_len = [], 0
            for i in range(0, ll, limit):
                chunks.append(line[i : i + limit])
            continue
        if current_len + ll > limit and current:
            chunks.append("".join(current))
            current = [line]
            current_len = ll
        else:
            current.append(line)
            current_len += ll
    if current:
        chunks.append("".join(current))
    return chunks


class DiscordAdapter(BaseChannelAdapter):
    platform = Platform.DISCORD
    max_message_length = 2000

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.token = config["bot_token"]
        intents = discord.Intents.default()
        intents.message_content = True  # required for reading message text
        intents.dm_messages = True
        intents.guild_messages = True
        self._client = discord.Client(intents=intents)
        self._bot_user_id: int | None = None
        self._client_task: asyncio.Task | None = None
        self._channel_cache: dict[str, discord.abc.Messageable] = {}
        self._ready_event = asyncio.Event()
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self._client.event
        async def on_ready() -> None:
            self._bot_user_id = self._client.user.id if self._client.user else None
            logger.info(
                "discord: connected as %s (id=%s)",
                self._client.user,
                self._bot_user_id,
            )
            self._ready_event.set()

        @self._client.event
        async def on_message(msg: discord.Message) -> None:
            # Skip our own messages (prevents echo loops)
            if self._bot_user_id is not None and msg.author.id == self._bot_user_id:
                return
            # Skip empty / non-text
            if not msg.content:
                return
            # Cache channel for later sends
            self._channel_cache[str(msg.channel.id)] = msg.channel
            event = MessageEvent(
                platform=Platform.DISCORD,
                chat_id=str(msg.channel.id),
                user_id=str(msg.author.id),
                text=msg.content,
                timestamp=(
                    msg.created_at.timestamp() if msg.created_at else time.time()
                ),
                metadata={"message_id": msg.id, "guild_id": msg.guild.id if msg.guild else None},
            )
            await self.handle_message(event)

    async def connect(self) -> bool:
        self._client_task = asyncio.create_task(self._client.start(self.token))
        # Wait briefly for on_ready (but don't block the whole gateway startup).
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=15.0)
            return True
        except TimeoutError:
            logger.warning("discord: connect timed out after 15s — continuing anyway")
            return False
        except Exception as e:  # noqa: BLE001
            logger.error("discord connect failed: %s", e)
            return False

    async def disconnect(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass
        if self._client_task is not None:
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):
                pass

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        channel = self._channel_cache.get(chat_id)
        if channel is None:
            # Try to fetch it
            try:
                channel = await self._client.fetch_channel(int(chat_id))
                self._channel_cache[chat_id] = channel
            except Exception as e:
                return SendResult(success=False, error=f"channel lookup failed: {e}")
        try:
            last_id: int | None = None
            for chunk in _chunk_2000(text, limit=self.max_message_length):
                sent = await channel.send(chunk)
                last_id = sent.id
        except Exception as e:
            return SendResult(success=False, error=f"{type(e).__name__}: {e}")
        return SendResult(success=True, message_id=str(last_id) if last_id else None)

    async def send_typing(self, chat_id: str) -> None:
        channel = self._channel_cache.get(chat_id)
        if channel is None:
            return
        try:
            await channel.typing().__aenter__()  # fire the typing indicator once
        except Exception:
            pass  # best effort


__all__ = ["DiscordAdapter", "_chunk_2000"]
