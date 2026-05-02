"""WeComFullAdapter — corp app channel for Tencent WeCom (企业微信).

Distinct from ``extensions/wecom-callback/`` (Group Chat Bot webhook —
simpler, outbound-only). This adapter uses the full corp-app credentials
(corp_id + agent_id + secret) and posts to ``qyapi.weixin.qq.com`` with
the rotated access_token in the URL query string.

Outbound endpoint:

    POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=...
    Body: {
        "touser":  "<UserAlice>",   # OR
        "toparty": "<dept-id>",     # OR
        "totag":   "<tag-id>",
        "msgtype": "text",
        "agentid": 1000001,
        "text":    {"content": "..."},
    }

The ``chat_id`` passed to :meth:`send` accepts a small DSL:

  - ``"UserAlice"``        — direct message via ``touser``
  - ``"party:42"``         — broadcast to department 42 via ``toparty``
  - ``"tag:eng-team"``     — broadcast to tag via ``totag``

This matches the WeCom corp-app surface; if no prefix is given, ``touser``
is the default.

Encrypted callback (WXBizMsgCrypt) for inbound is intentionally NOT in
this PR — that's its own ~200 LOC of crypto work. The plugin is
outbound-only at v1.
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
        "_wecom_full_token_cache", here / "token_cache.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_wecom_full_token_cache"] = mod
    spec.loader.exec_module(mod)
    return mod


_token_mod = _load_token_cache_module()
WeComTokenCache = _token_mod.WeComTokenCache


SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
SEND_TIMEOUT_SECONDS = 20.0

logger = logging.getLogger("opencomputer.ext.wecom_full")


def _parse_chat_id(chat_id: str) -> dict[str, str]:
    """Convert OC's chat_id DSL into WeCom's recipient fields.

    Returns one of:
      {"touser":  "..."}
      {"toparty": "..."}
      {"totag":   "..."}
    """
    if not chat_id:
        return {}
    if chat_id.startswith("party:"):
        return {"toparty": chat_id[len("party:"):]}
    if chat_id.startswith("tag:"):
        return {"totag": chat_id[len("tag:"):]}
    return {"touser": chat_id}


class WeComFullAdapter(BaseChannelAdapter):
    """Outbound corp-app channel for WeCom (企业微信).

    Outbound only at v1. Encrypted callback inbound is a focused follow-up.
    """

    platform = Platform.WECOM
    max_message_length = 2_000  # WeCom corp-app text cap
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._corp_id = (
            self.config.get("corp_id")
            or os.environ.get("WECOM_CORP_ID")
            or ""
        )
        self._agent_id = (
            self.config.get("agent_id")
            or os.environ.get("WECOM_AGENT_ID")
            or ""
        )
        self._secret = (
            self.config.get("secret")
            or os.environ.get("WECOM_SECRET")
            or ""
        )
        self._token_cache: Any = None
        self._connected = False

    async def connect(self) -> bool:
        if not self._corp_id or not self._secret or not self._agent_id:
            logger.error(
                "wecom (full): WECOM_CORP_ID + WECOM_AGENT_ID + WECOM_SECRET "
                "must all be set"
            )
            return False
        try:
            agent_int = int(self._agent_id)
            if agent_int <= 0:
                raise ValueError("agent_id must be a positive integer")
            self._agent_id_int = agent_int
        except (TypeError, ValueError) as exc:
            logger.error("wecom (full): invalid agent_id %r: %s", self._agent_id, exc)
            return False
        self._token_cache = WeComTokenCache(
            corp_id=self._corp_id, secret=self._secret
        )
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False
        self._token_cache = None

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        if not self._connected or not self._token_cache:
            return SendResult(success=False, error="not connected")

        recipient = _parse_chat_id(chat_id)
        if not recipient:
            return SendResult(
                success=False, error="wecom: chat_id is required"
            )

        try:
            token = self._token_cache.get_access_token()
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"wecom token fetch failed: {exc}")

        url = f"{SEND_URL}?access_token={token}"
        body: dict[str, Any] = {
            **recipient,
            "msgtype": "text",
            "agentid": self._agent_id_int,
            "text": {"content": text},
            "safe": 0,
        }
        try:
            response = httpx.post(url, json=body, timeout=SEND_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            return SendResult(success=False, error=f"wecom send failed: {exc}")
        if response.status_code != 200:
            return SendResult(
                success=False,
                error=(
                    f"wecom send returned {response.status_code}: "
                    f"{response.text[:200]}"
                ),
            )
        try:
            data: Any = response.json()
        except Exception:  # noqa: BLE001
            return SendResult(
                success=False, error=f"wecom send: non-JSON: {response.text[:200]}"
            )
        if not isinstance(data, dict):
            return SendResult(
                success=False, error=f"wecom send: malformed: {data!r}"
            )
        errcode = int(data.get("errcode", 0) or 0)
        if errcode != 0:
            return SendResult(
                success=False,
                error=f"wecom errcode {errcode}: {data.get('errmsg', 'unknown')}",
            )
        return SendResult(
            success=True,
            message_id=str(data.get("msgid") or ""),
        )


__all__ = ["WeComFullAdapter"]
