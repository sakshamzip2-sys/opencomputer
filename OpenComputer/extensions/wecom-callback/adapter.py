"""WeCom Callback channel adapter — outbound via Group Chat Bot webhook.

WeCom (企业微信 / Enterprise WeChat) provides "Group Chat Bot" webhooks
that post into a corporate group chat. This is the SIMPLER path; the
full WeCom integration (corp+agent+secret + access_token rotation +
encrypted callback) is a separate PR.

Out of scope (deferred to full WeCom adapter):
  - Send-as-app messaging (corp_id + agent_id + secret triple)
  - Inbound encrypted callback handling
  - User-targeted messages, media uploads, OAuth
  - The full WeCom plugin

Config:
  WECOM_WEBHOOK_URL  — required; from Group Chat Bot setup
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


class WeComCallbackAdapter(BaseChannelAdapter):
    platform = Platform.WECOM
    max_message_length = 4_096  # WeCom group bot text limit
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._webhook_url: str = (
            self.config.get("webhook_url")
            or os.environ.get("WECOM_WEBHOOK_URL")
            or ""
        )
        self._connected = False

    async def connect(self) -> bool:
        if not self._webhook_url:
            self._set_fatal_error(
                code="no_webhook_url",
                message="WECOM_WEBHOOK_URL is not set.",
                retryable=False,
            )
            return False
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        if not self._connected or not self._webhook_url:
            return SendResult(success=False, error="not connected")

        if len(text) > self.max_message_length:
            text = text[: self.max_message_length - 50] + "\n\n…[truncated]"

        payload = {
            "msgtype": "text",
            "text": {"content": text},
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self._webhook_url, json=payload)
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
            error=f"wecom error: {data.get('errmsg', response.text[:200])}",
        )
