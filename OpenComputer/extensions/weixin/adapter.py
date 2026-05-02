"""WeixinAdapter — Weixin Public Account (公众号 / Service Account) channel.

**Outbound** uses the Customer Service Message API:

    POST https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token=...
    Body: {"touser":"<openid>","msgtype":"text","text":{"content":"hello"}}

The ``chat_id`` passed to :meth:`send` is the recipient's openid — the
unique Weixin user identifier within the public account. Customer Service
messages have a 48-hour reply-window after the user's last interaction.

**Inbound** is via Weixin Server Configuration:

  1. User configures their public account "Server URL" pointing to OC's
     callback endpoint (a public HTTPS URL — typically through a tunnel
     in dev or a real cert in prod).
  2. Weixin sends GET requests with ``signature``, ``timestamp``, ``nonce``,
     ``echostr`` for the URL-verification handshake.
  3. After verification, Weixin POSTs XML message bodies to the same URL.
     Each XML carries ``MsgType``, ``Content`` (for text), ``FromUserName``
     (the user's openid), ``ToUserName`` (the bot's gh_xxxx).

The signature scheme is straightforward: SHA1 of the sorted concatenation
of (token, timestamp, nonce). The ``token`` is set by the user in Weixin's
Server Configuration UI and must match ``WEIXIN_TOKEN``.

This adapter currently ships outbound + inbound XML *parsing primitives*
(``verify_signature`` + ``parse_inbound_xml``). The actual HTTP listener
will land in a follow-up that uses the existing ``webhook-inbound``
plugin's aiohttp server pattern — XML callbacks are a different shape
than the JSON adapters there, so we keep them in their own module.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
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
        "_weixin_token_cache", here / "token_cache.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_weixin_token_cache"] = mod
    spec.loader.exec_module(mod)
    return mod


_token_mod = _load_token_cache_module()
WeixinTokenCache = _token_mod.WeixinTokenCache


SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
SEND_TIMEOUT_SECONDS = 20.0


logger = logging.getLogger("opencomputer.ext.weixin")


@dataclass
class WeixinInboundMessage:
    """A parsed inbound XML message from Weixin's callback."""
    text: str = ""
    from_openid: str = ""
    to_gh_account: str = ""
    msg_type: str = ""
    create_time: int = 0
    msg_id: str = ""
    raw_xml: bytes = b""


def verify_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    """Verify a Weixin GET handshake signature.

    SHA1 of sorted([token, timestamp, nonce]) joined → hex digest.
    """
    if not token or not timestamp or not nonce or not signature:
        return False
    items = sorted([token, timestamp, nonce])
    digest = hashlib.sha1("".join(items).encode("utf-8")).hexdigest()
    return digest == signature


def _xml_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def parse_inbound_xml(raw_xml: bytes) -> WeixinInboundMessage:
    """Parse Weixin's inbound XML callback body into a WeixinInboundMessage.

    Non-text MsgType values yield ``msg.text == ""`` but other fields
    (from_openid, msg_type, create_time) are still populated so the
    caller can decide whether to handle the event.
    """
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return WeixinInboundMessage(raw_xml=raw_xml)

    msg_type = _xml_text(root.find("MsgType"))
    return WeixinInboundMessage(
        text=_xml_text(root.find("Content")) if msg_type == "text" else "",
        from_openid=_xml_text(root.find("FromUserName")),
        to_gh_account=_xml_text(root.find("ToUserName")),
        msg_type=msg_type,
        create_time=int(_xml_text(root.find("CreateTime")) or 0),
        msg_id=_xml_text(root.find("MsgId")),
        raw_xml=raw_xml,
    )


class WeixinAdapter(BaseChannelAdapter):
    """Outbound Customer Service Message channel for Weixin Public Accounts."""

    platform = Platform.WEIXIN
    max_message_length = 2_000  # Weixin Customer Service text cap
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        self._appid = (
            self.config.get("appid")
            or os.environ.get("WEIXIN_APPID")
            or ""
        )
        self._secret = (
            self.config.get("secret")
            or os.environ.get("WEIXIN_SECRET")
            or ""
        )
        self._token_cache: Any = None
        self._connected = False

    async def connect(self) -> bool:
        if not self._appid or not self._secret:
            logger.error(
                "weixin: WEIXIN_APPID + WEIXIN_SECRET must both be set"
            )
            return False
        self._token_cache = WeixinTokenCache(
            appid=self._appid, secret=self._secret
        )
        # Don't fetch the token at connect — let the first send drive the
        # initial fetch so a misconfigured profile doesn't fail-fast on
        # a network error from Weixin's token endpoint.
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False
        self._token_cache = None

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send a text message to ``chat_id`` (a Weixin openid).

        Long messages are NOT chunked here — Weixin returns errcode 45002
        if you exceed 2000 chars. The caller can split into multiple sends
        if needed. (We keep ``max_message_length`` so the gateway can do
        the splitting upstream.)
        """
        if not self._connected or not self._token_cache:
            return SendResult(success=False, error="not connected")
        if not chat_id:
            return SendResult(
                success=False, error="weixin: chat_id (openid) is required"
            )

        try:
            token = self._token_cache.get_access_token()
        except Exception as exc:  # noqa: BLE001
            return SendResult(
                success=False, error=f"weixin token fetch failed: {exc}"
            )

        # Customer Service Message endpoint takes access_token in query string
        url = f"{SEND_URL}?access_token={token}"
        body = {
            "touser": chat_id,
            "msgtype": "text",
            "text": {"content": text},
        }
        try:
            response = httpx.post(
                url, json=body, timeout=SEND_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            return SendResult(
                success=False, error=f"weixin send failed: {exc}"
            )
        if response.status_code != 200:
            return SendResult(
                success=False,
                error=(
                    f"weixin send returned {response.status_code}: "
                    f"{response.text[:200]}"
                ),
            )
        try:
            data: Any = response.json()
        except Exception:  # noqa: BLE001
            return SendResult(
                success=False, error=f"weixin send: non-JSON response: {response.text[:200]}"
            )
        if not isinstance(data, dict):
            return SendResult(
                success=False, error=f"weixin send: malformed response: {data!r}"
            )
        errcode = int(data.get("errcode", 0) or 0)
        if errcode != 0:
            return SendResult(
                success=False,
                error=f"weixin errcode {errcode}: {data.get('errmsg', 'unknown')}",
            )
        return SendResult(success=True)


__all__ = [
    "WeixinAdapter",
    "WeixinInboundMessage",
    "parse_inbound_xml",
    "verify_signature",
]
