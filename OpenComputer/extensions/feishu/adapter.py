"""Feishu / Lark channel adapter — outbound via Custom Robot webhook.

Feishu (飞书) / Lark provides Custom Robot webhooks similar to DingTalk.
The signature scheme differs slightly — Feishu signs ``timestamp + secret``
with HMAC-SHA256 and base64-encodes, but the params go in the JSON body
not the URL.

Out of scope (deferred):
  - Inbound message receive (needs callback URL hosted by user)
  - Image/file uploads, interactive cards, mentions
  - App-level auth (this uses Custom Robot's simpler webhook auth)

Config:
  FEISHU_WEBHOOK_URL  — required; from Custom Robot setup
  FEISHU_SECRET       — optional; HMAC-SHA256 secret for signed requests
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any

import httpx

from plugin_sdk.channel_contract import (
    BaseChannelAdapter,
    ChannelCapabilities,
    SendResult,
)
from plugin_sdk.core import Platform


def _feishu_sign(timestamp: int, secret: str) -> str:
    """Feishu signature: HMAC-SHA256 of (timestamp + "\n" + secret) with
    secret as key, base64-encoded.
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


class FeishuAdapter(BaseChannelAdapter):
    platform = Platform.FEISHU
    max_message_length = 30_000  # Feishu text limit
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._webhook_url: str = (
            self.config.get("webhook_url")
            or os.environ.get("FEISHU_WEBHOOK_URL")
            or ""
        )
        self._secret: str = (
            self.config.get("secret")
            or os.environ.get("FEISHU_SECRET")
            or ""
        )
        self._connected = False

    async def connect(self) -> bool:
        if not self._webhook_url:
            self._set_fatal_error(
                code="no_webhook_url",
                message="FEISHU_WEBHOOK_URL is not set.",
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

        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self._secret:
            ts = int(time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = _feishu_sign(ts, self._secret)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(self._webhook_url, json=payload)
        except httpx.RequestError as e:
            return SendResult(success=False, error=f"network: {e}")

        try:
            data = response.json()
        except ValueError:
            data = {}

        # Feishu success: code=0 OR (older bots) StatusCode=0
        code = data.get("code", data.get("StatusCode", -1))
        if response.status_code == 200 and code == 0:
            return SendResult(success=True, message_id=None)
        return SendResult(
            success=False,
            error=f"feishu error: {data.get('msg', response.text[:200])}",
        )
