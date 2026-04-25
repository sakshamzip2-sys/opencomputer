"""iMessageAdapter — iMessage channel via the BlueBubbles bridge (G.16 / Tier 2.11).

BlueBubbles is a self-hosted Mac bridge (https://bluebubbles.app) that exposes
the iMessage database through HTTP + WebSocket APIs. We poll the HTTP endpoint
for new messages on a configurable interval (websocket support is a future
enhancement — polling is simpler and sufficient for personal use).

Setup (must be done on a Mac that's logged into iMessage):

1. Install BlueBubbles app from https://bluebubbles.app and let it scan iMessage.
2. Configure a server password in BlueBubbles → Settings → Server Settings.
3. Note the server URL (default ``http://localhost:1234``).
4. Set environment vars before starting OpenComputer::

       export BLUEBUBBLES_URL=http://localhost:1234
       export BLUEBUBBLES_PASSWORD=<password>

Mac-tied: the adapter only works when BlueBubbles is reachable, which means
the host must be a Mac running the BlueBubbles app. Won't work inside the
Docker image (Linux runtime). Consider a hybrid deployment: run the gateway
locally on Mac for iMessage + on VPS for everything else (different profiles).

Capabilities: text in/out + reactions (BlueBubbles ``api/v1/chat/<guid>/tap``).
File attachments + edit are technically possible via BlueBubbles but deferred
to G.16.x follow-ups.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.imessage")


_DEFAULT_POLL_INTERVAL = 10.0
_LIST_LIMIT = 50  # only consider the most recent N messages per poll


class IMessageAdapter(BaseChannelAdapter):
    """iMessage channel via BlueBubbles HTTP API.

    Polls ``GET /api/v1/message`` every ``poll_interval_seconds`` and tracks
    the highest message ROWID seen so far so we don't re-emit old messages.
    """

    platform = Platform.IMESSAGE
    max_message_length = 60_000
    capabilities = ChannelCapabilities.REACTIONS

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._base_url = str(config["base_url"]).rstrip("/")
        self._password = str(config["password"])
        self._poll_interval = float(config.get("poll_interval_seconds", _DEFAULT_POLL_INTERVAL))
        self._client: httpx.AsyncClient | None = None
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        # Track the highest ROWID we've already emitted so polling is idempotent.
        # ROWIDs are monotonic per BlueBubbles installation.
        self._last_rowid: int = 0
        # Self-detect from the bridge's "isFromMe" flag — don't echo our own
        # outbound messages back to the agent as inbound.

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(timeout=30.0)
        # Verify the bridge is reachable + password is correct.
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v1/server/info",
                params={"password": self._password},
            )
            if resp.status_code != 200:
                logger.error("BlueBubbles server/info HTTP %s: %s", resp.status_code, resp.text[:200])
                return False
            data = resp.json()
            logger.info(
                "imessage: connected to BlueBubbles %s",
                data.get("data", {}).get("server_version", "?"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("BlueBubbles connect failed: %s", exc)
            return False

        # Initialise high-watermark to the current latest ROWID so we don't
        # replay the entire iMessage history on first connect.
        try:
            self._last_rowid = await self._fetch_latest_rowid()
        except Exception as exc:  # noqa: BLE001
            logger.warning("imessage: failed to read initial rowid: %s — defaulting to 0", exc)
            self._last_rowid = 0

        self._poll_task = asyncio.create_task(self._poll_forever())
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client is not None:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Outbound — BlueBubbles HTTP send
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send a text message. ``chat_id`` is the BlueBubbles chat GUID."""
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v1/message/text",
                params={"password": self._password},
                json={
                    "chatGuid": chat_id,
                    "message": text[: self.max_message_length],
                    "method": "apple-script",
                },
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"bluebubbles HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            if data.get("status") not in (200, "success"):
                return SendResult(success=False, error=str(data.get("message") or data))
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def send_reaction(
        self, chat_id: str, message_id: str, emoji: str, **kwargs: Any
    ) -> SendResult:
        """Add a tapback reaction to a message via ``api/v1/message/react``.

        BlueBubbles maps a small set of emoji to iMessage tapbacks:
        ``"❤️" → love``, ``"👍" → like``, ``"👎" → dislike``,
        ``"😂" → laugh``, ``"❗️" → emphasize``, ``"❓" → question``.
        Other emoji return an error from the bridge.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        reaction = _emoji_to_tapback(emoji)
        if reaction is None:
            return SendResult(
                success=False,
                error=f"emoji {emoji!r} not mappable to iMessage tapback",
            )
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v1/message/react",
                params={"password": self._password},
                json={
                    "chatGuid": chat_id,
                    "selectedMessageGuid": message_id,
                    "reaction": reaction,
                },
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"bluebubbles HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            if data.get("status") not in (200, "success"):
                return SendResult(success=False, error=str(data.get("message") or data))
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("imessage poll error: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
                return
            except TimeoutError:
                continue

    async def _poll_once(self) -> None:
        events = await self._fetch_new_messages()
        for ev in events:
            await self.handle_message(ev)

    async def _fetch_latest_rowid(self) -> int:
        assert self._client is not None
        resp = await self._client.get(
            f"{self._base_url}/api/v1/message/query",
            params={"password": self._password, "limit": 1, "offset": 0, "sort": "DESC"},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", []) or []
        if not items:
            return 0
        return int(items[0].get("ROWID", 0))

    async def _fetch_new_messages(self) -> list[MessageEvent]:
        assert self._client is not None
        resp = await self._client.get(
            f"{self._base_url}/api/v1/message/query",
            params={"password": self._password, "limit": _LIST_LIMIT, "offset": 0, "sort": "DESC"},
        )
        if resp.status_code != 200:
            logger.warning("imessage query HTTP %s", resp.status_code)
            return []
        data = resp.json()
        items = data.get("data", []) or []
        events: list[MessageEvent] = []
        max_rowid = self._last_rowid
        for raw in items:
            rowid = int(raw.get("ROWID", 0))
            if rowid <= self._last_rowid:
                continue
            max_rowid = max(max_rowid, rowid)
            ev = self._parse_message(raw)
            if ev is not None:
                events.append(ev)
        self._last_rowid = max_rowid
        # Newest-first → oldest-first so the agent processes in chronological order.
        events.reverse()
        return events

    def _parse_message(self, raw: dict[str, Any]) -> MessageEvent | None:
        # Skip messages we sent ourselves (echoes)
        if raw.get("isFromMe"):
            return None
        text = raw.get("text") or ""
        chat_guid = ""
        chats = raw.get("chats") or []
        if chats:
            chat_guid = chats[0].get("guid", "") or ""
        if not chat_guid:
            return None
        if not text:
            return None
        handle = raw.get("handle") or {}
        sender = handle.get("address") or "unknown"
        date_ms = raw.get("dateCreated") or 0
        ts = float(date_ms) / 1000.0 if date_ms else time.time()
        return MessageEvent(
            platform=Platform.IMESSAGE,
            chat_id=chat_guid,
            user_id=sender,
            text=text,
            timestamp=ts,
            metadata={
                "imessage_guid": raw.get("guid"),
                "imessage_rowid": raw.get("ROWID"),
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# BlueBubbles tapback names per https://bluebubbles.app/api/
_EMOJI_TAPBACK_MAP = {
    "❤️": "love",
    "❤": "love",
    "👍": "like",
    "👍🏻": "like",
    "👎": "dislike",
    "😂": "laugh",
    "😆": "laugh",
    "❗️": "emphasize",
    "❗": "emphasize",
    "❓": "question",
    "❔": "question",
}


def _emoji_to_tapback(emoji: str) -> str | None:
    """Map an emoji to a BlueBubbles tapback name. Returns None if unmappable."""
    return _EMOJI_TAPBACK_MAP.get(emoji)


__all__ = ["IMessageAdapter", "_emoji_to_tapback"]
