"""SlackAdapter — Slack channel via the Web API (G.17 / Tier 2.12).

Outbound + reactions / edit / delete via raw httpx calls to the Slack
Web API. Inbound is intentionally NOT in this adapter — Slack inbound
requires Socket Mode (a heavyweight WebSocket client) or a public URL
for the Events API. Users wanting inbound should:

1. Set up Slack Outgoing Webhooks pointing at an OC webhook token (G.3).
2. The webhook adapter receives the POST, dispatches to the agent.
3. Agent's response goes back via this adapter's ``send``.

This keeps Slack support lightweight (no extra deps, no Socket Mode
runtime) while still enabling the most common use case: "agent posts
to a Slack channel".

Capabilities: REACTIONS, EDIT_MESSAGE, DELETE_MESSAGE.

Setup:

1. Create a Slack app at https://api.slack.com/apps.
2. Add Bot Token Scopes: ``chat:write``, ``reactions:write``, ``chat:write.public``.
3. Install to workspace, copy the Bot User OAuth Token (starts ``xoxb-``).
4. Set ``SLACK_BOT_TOKEN`` in OC's environment.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult
from plugin_sdk.format_converters import slack_mrkdwn

logger = logging.getLogger("opencomputer.ext.slack")


_SLACK_API_BASE = "https://slack.com/api"


class SlackAdapter(BaseChannelAdapter):
    """Slack channel — Web API only (no Socket Mode runtime)."""

    platform = Platform.SLACK
    max_message_length = 40_000  # Slack's per-block_text limit; chat.postMessage allows up to ~40k
    capabilities = (
        ChannelCapabilities.REACTIONS
        | ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.DELETE_MESSAGE
        | ChannelCapabilities.THREADS
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._token = config["bot_token"]
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> bool:
        """Connect = verify the bot token is valid via auth.test."""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            resp = await self._client.post(f"{_SLACK_API_BASE}/auth.test")
            data = resp.json()
            if not data.get("ok"):
                logger.error("slack auth.test failed: %s", data.get("error"))
                return False
            logger.info(
                "slack: connected as %s in workspace %s",
                data.get("user"),
                data.get("team"),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("slack connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Format-message — markdown → Slack mrkdwn (PR 3b.2)
    # ------------------------------------------------------------------

    def format_message(self, text: str) -> str:
        """Convert generic markdown into Slack mrkdwn.

        ``**bold**`` → ``*bold*``, ``[label](url)`` → ``<url|label>``,
        code fences preserved, etc. The converter falls back to plain
        text on parse error so a malformed input never crashes send.
        """
        return slack_mrkdwn.convert(text or "")

    # ------------------------------------------------------------------
    # Outbound — chat.postMessage
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send a message to a channel id (``C…``) or DM id (``D…``).

        ``kwargs`` may include:
        - ``thread_ts``: post as a threaded reply.
        - ``broadcast``: when threading, also broadcast to channel.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        formatted = self.format_message(text or "")
        payload: dict[str, Any] = {
            "channel": chat_id,
            "text": formatted[: self.max_message_length],
        }
        if kwargs.get("thread_ts"):
            payload["thread_ts"] = kwargs["thread_ts"]
            if kwargs.get("broadcast"):
                payload["reply_broadcast"] = True

        async def _do_send() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/chat.postMessage",
                    json=payload,
                )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True, message_id=str(data.get("ts") or ""))
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_send)

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def send_reaction(
        self, chat_id: str, message_id: str, emoji: str, **kwargs: Any
    ) -> SendResult:
        """Add an emoji reaction. Slack expects emoji NAMES (e.g. ``thumbsup``)
        not unicode codepoints — caller can pass either; we map common ones.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        name = _emoji_to_slack_name(emoji)

        async def _do_react() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/reactions.add",
                    json={"channel": chat_id, "timestamp": message_id, "name": name},
                )
                data = resp.json()
                if not data.get("ok"):
                    # already_reacted is harmless idempotent; surface as success
                    if data.get("error") == "already_reacted":
                        return SendResult(success=True)
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True)
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_react)

    # ------------------------------------------------------------------
    # Edit / Delete
    # ------------------------------------------------------------------

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        formatted = self.format_message(text or "")

        async def _do_edit() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/chat.update",
                    json={
                        "channel": chat_id,
                        "ts": message_id,
                        "text": formatted[: self.max_message_length],
                    },
                )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True, message_id=str(data.get("ts") or ""))
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_edit)

    async def delete_message(
        self, chat_id: str, message_id: str, **kwargs: Any
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")

        async def _do_delete() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/chat.delete",
                    json={"channel": chat_id, "ts": message_id},
                )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True)
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_delete)


# ---------------------------------------------------------------------------
# Emoji → Slack reaction name map
#
# Slack reactions use short-codes (``:thumbsup:``) rather than unicode emoji.
# Most callers will pass unicode (``"👍"``) so we map common ones; users can
# also pass the bare name (``"thumbsup"``) and we'll pass it through.
# ---------------------------------------------------------------------------

_EMOJI_TO_SLACK_NAME = {
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


def _emoji_to_slack_name(emoji_or_name: str) -> str:
    """Map a unicode emoji to its Slack reaction name. Bare-name input is passed through."""
    if not emoji_or_name:
        return ""
    # Already a slack name (no special chars)?
    if all(ch.isalnum() or ch in {"_", "-", "+"} for ch in emoji_or_name):
        return emoji_or_name.lower()
    return _EMOJI_TO_SLACK_NAME.get(emoji_or_name, emoji_or_name)


__all__ = ["SlackAdapter", "_emoji_to_slack_name"]
