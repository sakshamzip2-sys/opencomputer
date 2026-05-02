"""Tests for the webhook-inbound multi-platform signature verifiers.

Each platform has its own auth scheme — these tests assert the verifiers
accept correctly signed payloads and reject everything else.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_VERIFIERS_PY = (
    _REPO / "extensions" / "webhook-inbound" / "verifiers.py"
)


def _load():
    sys.modules.pop("webhook_inbound_verifiers", None)
    spec = importlib.util.spec_from_file_location(
        "webhook_inbound_verifiers", _VERIFIERS_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webhook_inbound_verifiers"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Teams Outgoing Webhook
# --------------------------------------------------------------------------- #

def _sign_teams(body: bytes, secret_b64: str) -> str:
    """Teams expects: Authorization: HMAC <base64(hmac_sha256(secret, body))>."""
    secret_bytes = base64.b64decode(secret_b64)
    digest = hmac.new(secret_bytes, body, hashlib.sha256).digest()
    return f"HMAC {base64.b64encode(digest).decode('ascii')}"


def test_teams_verifier_accepts_correct_hmac():
    mod = _load()
    body = b'{"text":"hi","from":{"name":"Alice"}}'
    secret_b64 = base64.b64encode(b"\x00" * 32).decode()  # 32-byte zero secret

    auth = _sign_teams(body, secret_b64)
    assert mod.verify_teams(auth, body, secret_b64) is True


def test_teams_verifier_rejects_wrong_hmac():
    mod = _load()
    body = b'{"text":"hi"}'
    secret_b64 = base64.b64encode(b"\x00" * 32).decode()
    assert mod.verify_teams("HMAC xxxxx", body, secret_b64) is False


def test_teams_verifier_rejects_missing_prefix():
    mod = _load()
    body = b"{}"
    secret_b64 = base64.b64encode(b"\x00" * 32).decode()
    correct = _sign_teams(body, secret_b64)
    raw_b64 = correct[len("HMAC "):]  # strip prefix
    assert mod.verify_teams(raw_b64, body, secret_b64) is False  # no "HMAC "


def test_teams_extracts_message_text():
    mod = _load()
    payload = {"text": "@bot tell me a joke", "from": {"name": "Alice", "id": "u-1"}}
    msg = mod.extract_teams_message(payload)
    assert msg.text == "@bot tell me a joke"
    assert msg.sender_name == "Alice"
    assert msg.sender_id == "u-1"


# --------------------------------------------------------------------------- #
# DingTalk Outgoing
# --------------------------------------------------------------------------- #

def _sign_dingtalk(timestamp_ms: int, secret: str) -> str:
    """DingTalk: HMAC-SHA256 of "{ts}\n{secret}" using secret as key, b64."""
    string_to_sign = f"{timestamp_ms}\n{secret}".encode()
    digest = hmac.new(
        secret.encode(), string_to_sign, hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def test_dingtalk_verifier_accepts_correct_signature():
    mod = _load()
    ts = "1700000000000"
    secret = "SECDingTalk"
    sig = _sign_dingtalk(int(ts), secret)
    assert mod.verify_dingtalk(ts, sig, secret) is True


def test_dingtalk_verifier_rejects_bad_signature():
    mod = _load()
    assert mod.verify_dingtalk("1700000000000", "wrong-sig", "SEC") is False


def test_dingtalk_verifier_rejects_missing_timestamp():
    mod = _load()
    assert mod.verify_dingtalk("", "any", "SEC") is False


def test_dingtalk_extracts_message_text():
    mod = _load()
    payload = {
        "msgtype": "text",
        "text": {"content": "@bot hello"},
        "senderNick": "Alice",
        "senderId": "u-1",
    }
    msg = mod.extract_dingtalk_message(payload)
    assert msg.text == "@bot hello"
    assert msg.sender_name == "Alice"
    assert msg.sender_id == "u-1"


# --------------------------------------------------------------------------- #
# Feishu Custom Bot Callback
# --------------------------------------------------------------------------- #

def _sign_feishu(timestamp: str, secret: str) -> str:
    """Feishu: HMAC-SHA256 of empty body using "{ts}\n{secret}" as key, b64."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode(), b"", hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def test_feishu_verifier_accepts_correct_signature():
    mod = _load()
    ts = "1700000000"
    secret = "SECFeishu"
    sig = _sign_feishu(ts, secret)
    assert mod.verify_feishu(ts, sig, secret) is True


def test_feishu_verifier_rejects_bad_signature():
    mod = _load()
    assert mod.verify_feishu("1700000000", "bad", "SEC") is False


def test_feishu_url_verification_extracts_challenge():
    """Feishu sends {type:'url_verification', challenge:'xyz'} on first call."""
    mod = _load()
    payload = {"type": "url_verification", "challenge": "abc-123"}
    challenge = mod.extract_feishu_challenge(payload)
    assert challenge == "abc-123"


def test_feishu_url_verification_returns_none_for_event_payload():
    mod = _load()
    payload = {"type": "event_callback", "event": {"text_without_at_bot": "hi"}}
    assert mod.extract_feishu_challenge(payload) is None


def test_feishu_extracts_message_text():
    mod = _load()
    payload = {
        "type": "event_callback",
        "event": {
            "message": {"content": '{"text":"@bot help"}'},
            "sender": {"sender_id": {"open_id": "ou-1"}},
        },
    }
    msg = mod.extract_feishu_message(payload)
    assert msg.text == "@bot help"
    assert msg.sender_id == "ou-1"


def test_inbound_message_dataclass_fields():
    mod = _load()
    msg = mod.InboundMessage(
        text="hello",
        sender_id="u-1",
        sender_name="Alice",
        platform="teams",
        chat_id="ch-1",
        raw={},
    )
    assert msg.text == "hello"
    assert msg.platform == "teams"
