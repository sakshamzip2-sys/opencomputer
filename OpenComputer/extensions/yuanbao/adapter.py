"""Tencent Yuanbao channel adapter — outbound REST webhook.

Yuanbao (元宝) is Tencent's consumer AI assistant with bot/webhook
support for posting messages. Hermes treats it as a generic webhook
target with optional API key in Authorization header.

Out of scope (deferred):
  - Inbound message receive (needs OAuth-secured callback URL)
  - Group/channel routing, mentions
  - Media uploads

Config:
  YUANBAO_WEBHOOK_URL  — required; bot/webhook URL
  YUANBAO_API_KEY      — optional; used as Authorization: Bearer
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


class YuanbaoAdapter(BaseChannelAdapter):
    platform = Platform.YUANBAO
    max_message_length = 8_000
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._webhook_url: str = (
            self.config.get("webhook_url")
            or os.environ.get("YUANBAO_WEBHOOK_URL")
            or ""
        )
        self._api_key: str = (
            self.config.get("api_key")
            or os.environ.get("YUANBAO_API_KEY")
            or ""
        )
        self._connected = False

    async def connect(self) -> bool:
        if not self._webhook_url:
            self._set_fatal_error(
                code="no_webhook_url",
                message="YUANBAO_WEBHOOK_URL is not set.",
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
            "type": "text",
            "content": text,
            "chat_id": chat_id,
        }
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    self._webhook_url, json=payload, headers=headers,
                )
        except httpx.RequestError as e:
            return SendResult(success=False, error=f"network: {e}")

        if response.status_code in (200, 201, 202):
            return SendResult(success=True, message_id=None)
        return SendResult(
            success=False,
            error=f"yuanbao returned {response.status_code}: {response.text[:200]}",
        )
