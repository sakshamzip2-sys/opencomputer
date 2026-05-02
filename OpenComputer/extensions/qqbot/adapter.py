"""QQBotAdapter — Tencent QQ Bot Open Platform channel.

Outbound endpoints:
  POST https://api.sgroup.qq.com/v2/groups/{group_openid}/messages
  POST https://api.sgroup.qq.com/v2/users/{user_openid}/messages

Authorization header:
  ``QQBot {appid}.{bot_access_token}``  (bot_access_token rotated by
  token_cache.py against bots.qq.com).

Body for a text message:
  {"content": "...", "msg_type": 0}

The :meth:`send` chat_id DSL:

  - ``"<group_openid>"``        — group message (default)
  - ``"user:<openid>"``         — direct message to user

Inbound WebSocket gateway (``wss://api.sgroup.qq.com/websocket/``) is a
focused follow-up. v1 is outbound-only.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path as _Path
from typing import Any

import httpx

from plugin_sdk.channel_contract import (
    BaseChannelAdapter,
    ChannelCapabilities,
    SendResult,
)
from plugin_sdk.core import Platform


def _load_token_cache_module() -> Any:
    here = _Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "_qqbot_token_cache", here / "token_cache.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_qqbot_token_cache"] = mod
    spec.loader.exec_module(mod)
    return mod


_token_mod = _load_token_cache_module()
QQBotTokenCache = _token_mod.QQBotTokenCache


API_HOST = "https://api.sgroup.qq.com"
SEND_TIMEOUT_SECONDS = 20.0

logger = logging.getLogger("opencomputer.ext.qqbot")


def _build_send_url(chat_id: str) -> str:
    """Map chat_id to the right /v2 message endpoint."""
    if chat_id.startswith("user:"):
        openid = chat_id[len("user:"):]
        return f"{API_HOST}/v2/users/{openid}/messages"
    return f"{API_HOST}/v2/groups/{chat_id}/messages"


class QQBotAdapter(BaseChannelAdapter):
    """Outbound QQ Bot Open Platform channel."""

    platform = Platform.QQBOT
    max_message_length = 1_500  # QQ Bot text message practical limit
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._appid = (
            self.config.get("appid")
            or os.environ.get("QQBOT_APPID")
            or ""
        )
        self._secret = (
            self.config.get("secret")
            or os.environ.get("QQBOT_SECRET")
            or ""
        )
        self._token_cache: Any = None
        self._connected = False

    async def connect(self) -> bool:
        if not self._appid or not self._secret:
            logger.error("qqbot: QQBOT_APPID + QQBOT_SECRET must both be set")
            return False
        self._token_cache = QQBotTokenCache(
            appid=self._appid, secret=self._secret
        )
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False
        self._token_cache = None

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        if not self._connected or not self._token_cache:
            return SendResult(success=False, error="not connected")
        if not chat_id:
            return SendResult(
                success=False, error="qqbot: chat_id (group/user openid) required"
            )

        try:
            token = self._token_cache.get_access_token()
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"qqbot token fetch failed: {exc}")

        url = _build_send_url(chat_id)
        headers = {
            "Authorization": f"QQBot {self._appid}.{token}",
            "Content-Type": "application/json",
            "X-Union-Appid": self._appid,
        }
        body = {"content": text, "msg_type": 0}  # 0 = text
        try:
            response = httpx.post(
                url, headers=headers, json=body, timeout=SEND_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            return SendResult(success=False, error=f"qqbot send failed: {exc}")
        if response.status_code != 200:
            # QQ Bot puts the error in body text — surface it
            return SendResult(
                success=False,
                error=(
                    f"qqbot send returned {response.status_code}: "
                    f"{response.text[:200]}"
                ),
            )
        try:
            data: Any = response.json()
        except Exception:  # noqa: BLE001
            return SendResult(
                success=False, error=f"qqbot send non-JSON: {response.text[:200]}"
            )
        if not isinstance(data, dict):
            return SendResult(
                success=False, error=f"qqbot send malformed: {data!r}"
            )
        # Successful send returns ``{"id": "msg-id", ...}``. Errors return
        # ``{"code": ..., "message": ...}``.
        if "code" in data and int(data.get("code", 0) or 0) != 0:
            return SendResult(
                success=False,
                error=f"qqbot code {data['code']}: {data.get('message', 'unknown')}",
            )
        return SendResult(
            success=True,
            message_id=str(data.get("id") or ""),
        )


__all__ = ["QQBotAdapter"]
