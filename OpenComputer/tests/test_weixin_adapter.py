"""Tests for the Weixin (WeChat Public Account) channel adapter.

Covers:
  - access_token rotation (cache + refresh on near-expiry)
  - Customer Service message send (text + sender openid)
  - Inbound XML signature verification
  - Inbound XML message extraction
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
_TOKEN_PY = _REPO / "extensions" / "weixin" / "token_cache.py"
_ADAPTER_PY = _REPO / "extensions" / "weixin" / "adapter.py"


def _load(name: str, path: Path):
    sys.modules.pop(f"weixin_test_{name}", None)
    spec = importlib.util.spec_from_file_location(f"weixin_test_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"weixin_test_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# token_cache.py
# --------------------------------------------------------------------------- #

def test_fetch_access_token_calls_token_endpoint():
    mod = _load("token", _TOKEN_PY)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "tok-fresh",
        "expires_in": 7200,
    }

    with patch("httpx.get", return_value=mock_response) as called:
        token, expires_at = mod.fetch_access_token(appid="appA", secret="secB")
    assert token == "tok-fresh"
    assert expires_at > time.time() + 7000

    args, kwargs = called.call_args
    assert "cgi-bin/token" in args[0]
    assert kwargs["params"]["appid"] == "appA"
    assert kwargs["params"]["secret"] == "secB"
    assert kwargs["params"]["grant_type"] == "client_credential"


def test_fetch_access_token_raises_on_weixin_errcode():
    mod = _load("token", _TOKEN_PY)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"errcode": 40013, "errmsg": "invalid appid"}

    with patch("httpx.get", return_value=mock_response):
        with pytest.raises(RuntimeError, match="invalid appid"):
            mod.fetch_access_token(appid="bad", secret="x")


def test_token_cache_returns_cached_token_when_fresh():
    mod = _load("token", _TOKEN_PY)
    cache = mod.WeixinTokenCache(appid="a", secret="b")
    cache._access_token = "cached"
    cache._expires_at = time.time() + 3600

    with patch("httpx.get") as called:
        token = cache.get_access_token()

    assert token == "cached"
    called.assert_not_called()


def test_token_cache_refreshes_when_expired():
    mod = _load("token", _TOKEN_PY)
    cache = mod.WeixinTokenCache(appid="a", secret="b")
    cache._access_token = "stale"
    cache._expires_at = time.time() - 60  # expired

    fresh = MagicMock()
    fresh.status_code = 200
    fresh.json.return_value = {"access_token": "tok-fresh", "expires_in": 7200}
    with patch("httpx.get", return_value=fresh):
        token = cache.get_access_token()
    assert token == "tok-fresh"


def test_token_cache_refreshes_with_60s_skew():
    """Token expires in 30s — within skew, must refresh."""
    mod = _load("token", _TOKEN_PY)
    cache = mod.WeixinTokenCache(appid="a", secret="b")
    cache._access_token = "skew-test"
    cache._expires_at = time.time() + 30  # within 60s skew

    fresh = MagicMock()
    fresh.status_code = 200
    fresh.json.return_value = {"access_token": "tok-skew-refreshed", "expires_in": 7200}
    with patch("httpx.get", return_value=fresh):
        token = cache.get_access_token()
    assert token == "tok-skew-refreshed"


# --------------------------------------------------------------------------- #
# Inbound XML signature + extraction
# --------------------------------------------------------------------------- #

def test_verify_inbound_signature_correct():
    mod = _load("adapter", _ADAPTER_PY)
    import hashlib

    token = "TOKEN"
    timestamp = "1700000000"
    nonce = "abc123"
    items = sorted([token, timestamp, nonce])
    expected = hashlib.sha1("".join(items).encode()).hexdigest()
    assert mod.verify_signature(token, timestamp, nonce, expected) is True


def test_verify_inbound_signature_rejects_wrong():
    mod = _load("adapter", _ADAPTER_PY)
    assert mod.verify_signature("TOKEN", "1700000000", "abc", "wrong-sig") is False


def test_parse_text_xml_extracts_content_and_openid():
    mod = _load("adapter", _ADAPTER_PY)
    xml = (
        b"<xml>"
        b"<ToUserName><![CDATA[gh_xx]]></ToUserName>"
        b"<FromUserName><![CDATA[user-openid]]></FromUserName>"
        b"<CreateTime>1700000000</CreateTime>"
        b"<MsgType><![CDATA[text]]></MsgType>"
        b"<Content><![CDATA[hello bot]]></Content>"
        b"<MsgId>1234</MsgId>"
        b"</xml>"
    )
    msg = mod.parse_inbound_xml(xml)
    assert msg.text == "hello bot"
    assert msg.from_openid == "user-openid"
    assert msg.msg_type == "text"


def test_parse_xml_handles_non_text_msgtype():
    mod = _load("adapter", _ADAPTER_PY)
    xml = (
        b"<xml>"
        b"<ToUserName>gh_x</ToUserName>"
        b"<FromUserName>u-1</FromUserName>"
        b"<CreateTime>1700000000</CreateTime>"
        b"<MsgType><![CDATA[image]]></MsgType>"
        b"<PicUrl>http://example/x.jpg</PicUrl>"
        b"</xml>"
    )
    msg = mod.parse_inbound_xml(xml)
    assert msg.msg_type == "image"
    # No text content for image messages
    assert msg.text == ""


# --------------------------------------------------------------------------- #
# Adapter — outbound send
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_send_calls_customer_service_endpoint(monkeypatch):
    monkeypatch.setenv("WEIXIN_APPID", "appA")
    monkeypatch.setenv("WEIXIN_SECRET", "secB")
    mod = _load("adapter", _ADAPTER_PY)

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs.get("json") or json.loads(kwargs.get("content", b"").decode("utf-8") or "{}")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
        return resp

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"access_token": "tok-A", "expires_in": 7200}

    adapter = mod.WeixinAdapter(config={"appid": "appA", "secret": "secB"})
    assert await adapter.connect() is True

    with patch("httpx.get", return_value=fresh_token), \
         patch("httpx.post", side_effect=fake_post):
        result = await adapter.send(chat_id="user-openid-123", text="hello")

    assert result.success is True
    assert "message/custom/send" in captured["url"]
    assert captured["url"].endswith("access_token=tok-A")
    body = captured["body"]
    assert body["touser"] == "user-openid-123"
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "hello"


@pytest.mark.asyncio
async def test_send_propagates_weixin_errcode(monkeypatch):
    monkeypatch.setenv("WEIXIN_APPID", "appA")
    monkeypatch.setenv("WEIXIN_SECRET", "secB")
    mod = _load("adapter", _ADAPTER_PY)

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"access_token": "tok-A", "expires_in": 7200}

    bad_send = MagicMock()
    bad_send.status_code = 200
    bad_send.json.return_value = {"errcode": 45015, "errmsg": "response out of time limit"}

    adapter = mod.WeixinAdapter(config={"appid": "appA", "secret": "secB"})
    assert await adapter.connect() is True
    with patch("httpx.get", return_value=fresh_token), \
         patch("httpx.post", return_value=bad_send):
        result = await adapter.send(chat_id="u", text="x")
    assert result.success is False
    assert "out of time limit" in (result.error or "")


@pytest.mark.asyncio
async def test_connect_fails_without_credentials(monkeypatch):
    monkeypatch.delenv("WEIXIN_APPID", raising=False)
    monkeypatch.delenv("WEIXIN_SECRET", raising=False)
    mod = _load("adapter", _ADAPTER_PY)
    adapter = mod.WeixinAdapter(config={})
    assert await adapter.connect() is False


def test_plugin_manifest_exists():
    manifest_path = _REPO / "extensions" / "weixin" / "plugin.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kind"] == "channel"


def test_platform_weixin_added_to_enum():
    from plugin_sdk.core import Platform
    assert hasattr(Platform, "WEIXIN")
