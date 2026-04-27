"""MattermostAdapter — Mattermost channel via Web API (G.18 / Tier 3.x).

Mattermost is a self-hosted Slack alternative. Its Web API surface is
similar to Slack's; this adapter mirrors the G.17 Slack pattern:

- Outbound + reactions + edit + delete via raw httpx → ``/api/v4/...``.
- No WebSocket runtime — inbound via Mattermost Outgoing Webhooks → OC
  webhook adapter (G.3). Same minimal-dep tradeoff as Slack.

Setup:

1. Get a Personal Access Token from Mattermost (User Settings →
   Security → Personal Access Tokens). Need ``post:write`` scope.
2. Set ``MATTERMOST_URL`` (e.g. ``https://mm.example.com``) and
   ``MATTERMOST_TOKEN``. Disabled by default.

Capabilities: REACTIONS + EDIT_MESSAGE + DELETE_MESSAGE + THREADS.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult

logger = logging.getLogger("opencomputer.ext.mattermost")


class MattermostAdapter(BaseChannelAdapter):
    """Mattermost channel — Web API only (no WebSocket runtime)."""

    platform = Platform.MATTERMOST
    max_message_length = 16_000  # Mattermost server-default
    capabilities = (
        ChannelCapabilities.REACTIONS
        | ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.DELETE_MESSAGE
        | ChannelCapabilities.THREADS
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._base_url = str(config["base_url"]).rstrip("/")
        self._token = str(config["token"])
        self._user_id: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> bool:
        """Verify the token via ``users/me`` and cache the bot user id."""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            resp = await self._client.get(f"{self._base_url}/api/v4/users/me")
            if resp.status_code != 200:
                logger.error(
                    "mattermost users/me HTTP %s: %s",
                    resp.status_code, resp.text[:200],
                )
                return False
            data = resp.json()
            self._user_id = data.get("id")
            logger.info(
                "mattermost: connected as %s (id=%s)",
                data.get("username"), self._user_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("mattermost connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Outbound — POST /api/v4/posts
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send a message to a channel id. ``chat_id`` is the channel ID
        (a 26-char alphanumeric).

        ``kwargs`` may include ``root_id`` to thread under a parent post.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        payload: dict[str, Any] = {
            "channel_id": chat_id,
            "message": text[: self.max_message_length],
        }
        if kwargs.get("root_id"):
            payload["root_id"] = kwargs["root_id"]
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v4/posts", json=payload
            )
            if resp.status_code != 201:
                return SendResult(
                    success=False,
                    error=f"mattermost HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            return SendResult(success=True, message_id=str(data.get("id") or ""))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def send_reaction(
        self, chat_id: str, message_id: str, emoji: str, **kwargs: Any
    ) -> SendResult:
        """Add a reaction. Mattermost expects emoji NAMES (``thumbsup``)
        — same convention as Slack.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        if self._user_id is None:
            return SendResult(success=False, error="user_id not cached — connect first")

        name = _emoji_to_emoji_name(emoji)
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v4/reactions",
                json={
                    "user_id": self._user_id,
                    "post_id": message_id,
                    "emoji_name": name,
                },
            )
            if resp.status_code != 201:
                return SendResult(
                    success=False,
                    error=f"mattermost HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Edit / Delete
    # ------------------------------------------------------------------

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        try:
            resp = await self._client.put(
                f"{self._base_url}/api/v4/posts/{message_id}",
                json={
                    "id": message_id,
                    "message": text[: self.max_message_length],
                },
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"mattermost HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def delete_message(
        self, chat_id: str, message_id: str, **kwargs: Any
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        try:
            resp = await self._client.delete(
                f"{self._base_url}/api/v4/posts/{message_id}"
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"mattermost HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Emoji → emoji-name map (duplicated from Slack adapter — cross-plugin imports
# are forbidden, see tests/test_cross_plugin_isolation.py)
# ---------------------------------------------------------------------------

_EMOJI_TO_NAME = {
    "👍": "thumbsup",
    "👎": "thumbsdown",
    "❤️": "heart",
    "❤": "heart",
    "🎉": "tada",
    "🔥": "fire",
    "👀": "eyes",
    "✅": "white_check_mark",
    "❌": "x",
    "⚠️": "warning",
    "⚠": "warning",
    "🚀": "rocket",
    "💯": "100",
    "😂": "joy",
    "🤔": "thinking_face",
    "👏": "clap",
}


def _emoji_to_emoji_name(emoji_or_name: str) -> str:
    """Map a unicode emoji to its emoji-name. Bare-name input is passed through (lowercased)."""
    if not emoji_or_name:
        return ""
    if all(ch.isalnum() or ch in {"_", "-", "+"} for ch in emoji_or_name):
        return emoji_or_name.lower()
    return _EMOJI_TO_NAME.get(emoji_or_name, emoji_or_name)


__all__ = ["MattermostAdapter", "_emoji_to_emoji_name"]
