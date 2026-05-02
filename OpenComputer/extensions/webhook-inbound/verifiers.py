"""Per-platform inbound webhook signature verification + message extraction.

Each platform speaks its own auth dialect:

  - Teams Outgoing Webhook: ``Authorization: HMAC <base64(hmac_sha256(secret, body))>``
    Secret is provisioned as a base64 string when the user creates the
    Outgoing Webhook in Teams.

  - DingTalk Outgoing Bot: ``timestamp`` + ``sign`` headers. Sign string
    is ``{timestamp}\n{secret}``, HMAC-SHA256 with secret as key, then
    base64-encoded.

  - Feishu / Lark Custom Bot Callback: ``X-Lark-Request-Timestamp`` +
    ``X-Lark-Signature``. Sign string is ``{timestamp}\n{secret}``, key
    is THAT string, body is empty bytes — base64 of HMAC-SHA256.
    Feishu also sends a URL-verification handshake on first activation.

All three validations are constant-time (hmac.compare_digest) to prevent
timing leaks. Message extraction accepts only well-formed text messages —
any other shape is treated as not-a-text-message and skipped.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class InboundMessage:
    text: str = ""
    sender_id: str = ""
    sender_name: str = ""
    platform: str = ""
    chat_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #

def verify_teams(authorization_header: str, body: bytes, secret_b64: str) -> bool:
    """Verify a Teams Outgoing Webhook ``Authorization: HMAC <b64>`` header."""
    if not authorization_header or not authorization_header.startswith("HMAC "):
        return False
    provided = authorization_header[len("HMAC "):].strip()
    try:
        secret_bytes = base64.b64decode(secret_b64)
    except (binascii.Error, ValueError):
        return False
    expected_digest = hmac.new(secret_bytes, body, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected_digest).decode("ascii")
    return hmac.compare_digest(provided, expected_b64)


def extract_teams_message(payload: dict[str, Any]) -> InboundMessage:
    """Pull text + sender from a Teams Outgoing Webhook POST body."""
    text = str(payload.get("text") or "").strip()
    from_obj = payload.get("from") or {}
    if not isinstance(from_obj, dict):
        from_obj = {}
    return InboundMessage(
        text=text,
        sender_id=str(from_obj.get("id") or ""),
        sender_name=str(from_obj.get("name") or ""),
        platform="teams",
        raw=payload,
    )


# --------------------------------------------------------------------------- #
# DingTalk
# --------------------------------------------------------------------------- #

def verify_dingtalk(timestamp: str, sign: str, secret: str) -> bool:
    """Verify a DingTalk Outgoing Bot signature.

    String to sign: ``{timestamp}\\n{secret}``. Key: secret (utf-8). Body
    is empty for outgoing-bot signatures (DingTalk doesn't sign body).
    """
    if not timestamp or not sign or not secret:
        return False
    string_to_sign = f"{timestamp}\n{secret}".encode()
    expected = hmac.new(
        secret.encode(), string_to_sign, hashlib.sha256
    ).digest()
    expected_b64 = base64.b64encode(expected).decode("ascii")
    return hmac.compare_digest(sign, expected_b64)


def extract_dingtalk_message(payload: dict[str, Any]) -> InboundMessage:
    """Pull text + sender from a DingTalk Outgoing Bot POST body."""
    msg_type = str(payload.get("msgtype") or "")
    text = ""
    if msg_type == "text":
        text_obj = payload.get("text") or {}
        if isinstance(text_obj, dict):
            text = str(text_obj.get("content") or "").strip()
    return InboundMessage(
        text=text,
        sender_id=str(payload.get("senderId") or ""),
        sender_name=str(payload.get("senderNick") or ""),
        platform="dingtalk",
        chat_id=str(payload.get("conversationId") or ""),
        raw=payload,
    )


# --------------------------------------------------------------------------- #
# Feishu / Lark
# --------------------------------------------------------------------------- #

def verify_feishu(timestamp: str, sign: str, secret: str) -> bool:
    """Verify a Feishu Custom Bot Callback signature.

    String to sign: ``{timestamp}\\n{secret}``. Key: that string. Body:
    empty bytes. base64-encoded HMAC-SHA256 of an empty body.
    """
    if not timestamp or not sign or not secret:
        return False
    string_to_sign = f"{timestamp}\n{secret}"
    expected = hmac.new(
        string_to_sign.encode(), b"", hashlib.sha256
    ).digest()
    expected_b64 = base64.b64encode(expected).decode("ascii")
    return hmac.compare_digest(sign, expected_b64)


def extract_feishu_challenge(payload: dict[str, Any]) -> str | None:
    """If the payload is a URL-verification handshake, return the challenge."""
    if str(payload.get("type") or "") == "url_verification":
        challenge = payload.get("challenge")
        if isinstance(challenge, str):
            return challenge
    return None


def extract_feishu_message(payload: dict[str, Any]) -> InboundMessage:
    """Pull text + sender from a Feishu event_callback body."""
    event = payload.get("event") or {}
    if not isinstance(event, dict):
        event = {}
    message = event.get("message") or {}
    text = ""
    if isinstance(message, dict):
        content_raw = message.get("content")
        if isinstance(content_raw, str):
            try:
                content = json.loads(content_raw)
                if isinstance(content, dict):
                    text = str(content.get("text") or "").strip()
            except json.JSONDecodeError:
                text = ""

    sender_obj = event.get("sender") or {}
    sender_id_obj = sender_obj.get("sender_id") or {} if isinstance(sender_obj, dict) else {}
    sender_id = ""
    if isinstance(sender_id_obj, dict):
        sender_id = str(
            sender_id_obj.get("open_id")
            or sender_id_obj.get("user_id")
            or ""
        )

    chat_id = ""
    if isinstance(message, dict):
        chat_id = str(message.get("chat_id") or "")

    return InboundMessage(
        text=text,
        sender_id=sender_id,
        platform="feishu",
        chat_id=chat_id,
        raw=payload,
    )


__all__ = [
    "InboundMessage",
    "extract_dingtalk_message",
    "extract_feishu_challenge",
    "extract_feishu_message",
    "extract_teams_message",
    "verify_dingtalk",
    "verify_feishu",
    "verify_teams",
]
