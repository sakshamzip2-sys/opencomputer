"""DingTalk channel adapter — outbound via Custom Robot webhook.

DingTalk (钉钉) provides "Custom Robot" webhooks that post into a
group chat. The webhook URL is generated when you add a Custom Robot
to a group. Optionally a HMAC-SHA256 secret signs requests for extra
security.

What this adapter supports today:
  - send(chat_id, text) — POSTs a markdown message via the webhook
  - HMAC-SHA256 signature when DINGTALK_SECRET is set

What's deferred (separate PR):
  - Inbound messages — needs an outbound-callback URL that DingTalk
    POSTs to when users @mention the bot. Requires hosting an HTTP
    server on a public URL (or tunneling).
  - Image/file uploads, mentions, action cards.

Config:
  DINGTALK_WEBHOOK_URL — required; from Custom Robot setup
  DINGTALK_SECRET      — optional; HMAC-SHA256 secret for signed requests
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from typing import Any

import httpx

from plugin_sdk.channel_contract import (
    BaseChannelAdapter,
    ChannelCapabilities,
    SendResult,
)
from plugin_sdk.core import Platform


def _sign_url(webhook_url: str, secret: str) -> str:
    """Apply HMAC-SHA256 signature to a DingTalk webhook URL.

    DingTalk's signature scheme: timestamp + "\n" + secret, HMAC-SHA256
    with secret as key, base64-encoded, then URL-encoded.
    """
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"


class DingTalkAdapter(BaseChannelAdapter):
    """Outbound-only DingTalk adapter via Custom Robot webhook."""

    platform = Platform.DINGTALK
    max_message_length = 5_000  # DingTalk markdown soft limit
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._webhook_url: str = (
            self.config.get("webhook_url")
            or os.environ.get("DINGTALK_WEBHOOK_URL")
            or ""
        )
        self._secret: str = (
            self.config.get("secret")
            or os.environ.get("DINGTALK_SECRET")
            or ""
        )
        self._connected = False

    async def connect(self) -> bool:
        if not self._webhook_url:
            self._set_fatal_error(
                code="no_webhook_url",
                message="DINGTALK_WEBHOOK_URL is not set. "
                        "Add a Custom Robot to your DingTalk group and copy the URL.",
                retryable=False,
            )
            return False
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Post markdown text to DingTalk group via Custom Robot webhook.

        ``chat_id`` is informational; webhooks are group-scoped.
        Title defaults to "Message"; can be overridden via kwargs['title'].
        """
        if not self._connected or not self._webhook_url:
            return SendResult(success=False, error="not connected")

        if len(text) > self.max_message_length:
            text = text[: self.max_message_length - 50] + "\n\n…[truncated]"

        url = _sign_url(self._webhook_url, self._secret) if self._secret else self._webhook_url
        title = kwargs.get("title", "Message")

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload)
        except httpx.RequestError as e:
            return SendResult(success=False, error=f"network: {e}")

        try:
            data = response.json()
        except ValueError:
            data = {}

        if response.status_code == 200 and data.get("errcode", -1) == 0:
            return SendResult(success=True, message_id=None)
        return SendResult(
            success=False,
            error=f"dingtalk error: {data.get('errmsg', response.text[:200])}",
        )
