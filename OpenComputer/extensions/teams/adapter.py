"""Microsoft Teams channel adapter — outbound via Incoming Webhook.

The webhook approach is the simplest way to post into a Teams channel:
1. In Teams, add the "Incoming Webhook" connector to a channel
2. Copy the URL to TEAMS_WEBHOOK_URL
3. POST JSON to that URL → message appears in the channel

What this adapter supports today:
  - send(chat_id, text)  — POSTs an Adaptive Card-style message to
    the webhook. ``chat_id`` is informational; webhooks are
    channel-scoped (one URL per Teams channel).

What's deferred (separate PR):
  - Inbound messages — needs the Bot Framework SDK or Microsoft Graph
    polling, plus an Azure Bot registration. That's substantial new
    surface area and gated on user demand.
  - DMs to specific users — requires Bot Framework.
  - Threaded replies, reactions, mentions — Bot Framework features.

Config:
  TEAMS_WEBHOOK_URL  — required; full Incoming Webhook URL from your
                       Teams channel's connector setup.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from plugin_sdk.channel_contract import (
    BaseChannelAdapter,
    ChannelCapabilities,
    SendResult,
)
from plugin_sdk.core import Platform


class TeamsAdapter(BaseChannelAdapter):
    """Outbound-only Teams adapter via Incoming Webhook."""

    platform = Platform.TEAMS
    max_message_length = 28_000  # MS Teams Adaptive Card practical limit
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._webhook_url: str = (
            self.config.get("webhook_url")
            or os.environ.get("TEAMS_WEBHOOK_URL")
            or ""
        )
        self._connected = False

    async def connect(self) -> bool:
        """No persistent connection; verify the webhook URL is set."""
        if not self._webhook_url:
            self._set_fatal_error(
                code="no_webhook_url",
                message="TEAMS_WEBHOOK_URL is not set. "
                        "Configure an Incoming Webhook in your Teams channel.",
                retryable=False,
            )
            return False
        self._connected = True
        return True

    async def disconnect(self) -> None:
        """Nothing to disconnect from — webhooks are stateless."""
        self._connected = False

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Post text to the Teams channel via Incoming Webhook.

        ``chat_id`` is ignored — webhook URL is channel-scoped. Long
        text is sent as a single Adaptive Card (Teams accepts up to
        ~28k chars per message).
        """
        if not self._connected or not self._webhook_url:
            return SendResult(success=False, error="not connected")

        # Truncate if absurdly long — log a warning instead of failing
        if len(text) > self.max_message_length:
            text = text[: self.max_message_length - 100] + (
                "\n\n…[truncated for Teams limit]"
            )

        payload = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }],
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self._webhook_url, json=payload)
        except httpx.RequestError as e:
            return SendResult(success=False, error=f"network: {e}")

        if response.status_code in (200, 201, 202):
            return SendResult(success=True, message_id=None)
        return SendResult(
            success=False,
            error=f"webhook returned {response.status_code}: {response.text[:200]}",
        )
