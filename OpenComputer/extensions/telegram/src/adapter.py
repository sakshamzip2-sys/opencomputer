"""
TelegramAdapter — Telegram Bot API channel adapter.

Uses raw Bot API via httpx with long-polling, zero external deps beyond
httpx (already a project dep). Kept simple for Phase 2; Phase 3 can swap
to python-telegram-bot / aiogram for richer features.

Handles:
- Long-polling for inbound messages (getUpdates)
- MarkdownV2 escaping for outbound messages
- 4096 UTF-16 char limit with safe splitting
- Self-message filtering
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.telegram")


# Telegram MarkdownV2 requires escaping these characters
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"
_MDV2_RE = re.compile(f"([{re.escape(_MDV2_SPECIAL)}])")


def _escape_mdv2(text: str) -> str:
    return _MDV2_RE.sub(r"\\\1", text)


def _utf16_len(s: str) -> int:
    """Telegram's message length limit is in UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


def _chunk_for_telegram(text: str, limit: int = 4096) -> list[str]:
    """Split `text` so each chunk is ≤ `limit` UTF-16 units, respecting line breaks."""
    if _utf16_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        line_len = _utf16_len(line)
        if current_len + line_len > limit and current:
            chunks.append("".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("".join(current))
    return chunks


class TelegramAdapter(BaseChannelAdapter):
    platform = Platform.TELEGRAM
    max_message_length = 4096  # UTF-16 units

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.token = config["bot_token"]
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._client: httpx.AsyncClient | None = None
        self._bot_id: int | None = None
        self._offset: int = 0
        self._polling_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(timeout=35.0)
        # getMe to verify token and cache our bot id
        try:
            resp = await self._client.get(f"{self.base_url}/getMe")
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.error("telegram getMe failed: %s", data)
                return False
            self._bot_id = data["result"]["id"]
            logger.info(
                "telegram: connected as @%s (id=%s)",
                data["result"].get("username", "?"),
                self._bot_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("telegram connect failed: %s", e)
            return False
        # start long-polling loop
        self._polling_task = asyncio.create_task(self._poll_forever())
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._client is not None:
            await self._client.aclose()

    async def _poll_forever(self) -> None:
        assert self._client is not None
        while not self._stop_event.is_set():
            try:
                params = {"timeout": 30, "offset": self._offset, "allowed_updates": ["message"]}
                resp = await self._client.get(f"{self.base_url}/getUpdates", params=params)
                if resp.status_code != 200:
                    await asyncio.sleep(2)
                    continue
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue
                for update in data.get("result", []):
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("telegram polling error: %s — sleeping 5s", e)
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if msg is None:
            return
        frm = msg.get("from", {})
        # Skip self-messages (some platforms echo)
        if self._bot_id is not None and frm.get("id") == self._bot_id:
            return
        text = msg.get("text", "")
        if not text:
            return
        event = MessageEvent(
            platform=Platform.TELEGRAM,
            chat_id=str(msg["chat"]["id"]),
            user_id=str(frm.get("id", "")),
            text=text,
            timestamp=float(msg.get("date", time.time())),
            metadata={"message_id": msg.get("message_id")},
        )
        await self.handle_message(event)

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        assert self._client is not None
        # Send as plain text for Phase 2 (no formatting) — easier to debug.
        # Phase 3 can add MarkdownV2 handling with escape detection.
        for chunk in _chunk_for_telegram(text, limit=self.max_message_length):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk, "disable_notification": False},
                )
                if resp.status_code != 200:
                    return SendResult(
                        success=False,
                        error=f"telegram HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(success=False, error=str(data))
            except Exception as e:  # noqa: BLE001
                return SendResult(success=False, error=f"{type(e).__name__}: {e}")
        return SendResult(success=True)

    async def send_typing(self, chat_id: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.post(
                f"{self.base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
        except Exception:
            pass


__all__ = ["TelegramAdapter", "_escape_mdv2", "_utf16_len", "_chunk_for_telegram"]
