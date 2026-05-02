"""Tests for the full WeCom (企业微信) channel adapter.

Distinct from extensions/wecom-callback/ (Group Chat Bot webhook).
This adapter uses corp_id + agent_id + secret + access_token rotation
against qyapi.weixin.qq.com.
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
_TOKEN_PY = _REPO / "extensions" / "wecom" / "token_cache.py"
_ADAPTER_PY = _REPO / "extensions" / "wecom" / "adapter.py"


def _load(name: str, path: Path):
    sys.modules.pop(f"wecom_full_test_{name}", None)
    spec = importlib.util.spec_from_file_location(f"wecom_full_test_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"wecom_full_test_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# token_cache.py
# --------------------------------------------------------------------------- #

def test_fetch_access_token_calls_gettoken_endpoint():
    mod = _load("token", _TOKEN_PY)
    fresh = MagicMock()
    fresh.status_code = 200
    fresh.json.return_value = {
        "errcode": 0,
        "errmsg": "ok",
        "access_token": "tok-wecom",
        "expires_in": 7200,
    }
    with patch("httpx.get", return_value=fresh) as called:
        token, expires_at = mod.fetch_access_token(corp_id="corpA", secret="secB")
    assert token == "tok-wecom"
    assert expires_at > time.time() + 7000
    args, kwargs = called.call_args
    assert "qyapi.weixin.qq.com" in args[0]
    assert "/cgi-bin/gettoken" in args[0]
    assert kwargs["params"]["corpid"] == "corpA"
    assert kwargs["params"]["corpsecret"] == "secB"


def test_fetch_access_token_raises_on_errcode():
    mod = _load("token", _TOKEN_PY)
    bad = MagicMock()
    bad.status_code = 200
    bad.json.return_value = {"errcode": 40013, "errmsg": "invalid corpid"}
    with patch("httpx.get", return_value=bad):
        with pytest.raises(RuntimeError, match="invalid corpid"):
            mod.fetch_access_token(corp_id="bad", secret="x")


def test_token_cache_refreshes_when_expired():
    mod = _load("token", _TOKEN_PY)
    cache = mod.WeComTokenCache(corp_id="a", secret="b")
    cache._access_token = "stale"
    cache._expires_at = time.time() - 30
    fresh = MagicMock()
    fresh.status_code = 200
    fresh.json.return_value = {"errcode": 0, "access_token": "fresh", "expires_in": 7200}
    with patch("httpx.get", return_value=fresh):
        token = cache.get_access_token()
    assert token == "fresh"


def test_token_cache_returns_cached_when_fresh():
    mod = _load("token", _TOKEN_PY)
    cache = mod.WeComTokenCache(corp_id="a", secret="b")
    cache._access_token = "cached"
    cache._expires_at = time.time() + 3600
    with patch("httpx.get") as called:
        token = cache.get_access_token()
    assert token == "cached"
    called.assert_not_called()


# --------------------------------------------------------------------------- #
# Adapter — outbound send
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_send_text_to_corp_user(monkeypatch):
    monkeypatch.setenv("WECOM_CORP_ID", "corpA")
    monkeypatch.setenv("WECOM_AGENT_ID", "1000001")
    monkeypatch.setenv("WECOM_SECRET", "secB")

    mod = _load("adapter", _ADAPTER_PY)

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["body"] = kwargs.get("json")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"errcode": 0, "errmsg": "ok", "msgid": "MSG-1"}
        return resp

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"errcode": 0, "access_token": "tok-X", "expires_in": 7200}

    adapter = mod.WeComFullAdapter(config={})
    assert await adapter.connect() is True
    with patch("httpx.get", return_value=fresh_token), \
         patch("httpx.post", side_effect=fake_post):
        result = await adapter.send(chat_id="UserAlice", text="hello")

    assert result.success is True
    assert result.message_id == "MSG-1"
    assert "qyapi.weixin.qq.com" in captured["url"]
    assert "/cgi-bin/message/send" in captured["url"]
    body = captured["body"]
    assert body["touser"] == "UserAlice"
    assert body["msgtype"] == "text"
    assert body["agentid"] == 1000001
    assert body["text"]["content"] == "hello"


@pytest.mark.asyncio
async def test_send_propagates_errcode(monkeypatch):
    monkeypatch.setenv("WECOM_CORP_ID", "corpA")
    monkeypatch.setenv("WECOM_AGENT_ID", "1000001")
    monkeypatch.setenv("WECOM_SECRET", "secB")
    mod = _load("adapter", _ADAPTER_PY)

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"errcode": 0, "access_token": "tok-X", "expires_in": 7200}
    bad_send = MagicMock()
    bad_send.status_code = 200
    bad_send.json.return_value = {"errcode": 81013, "errmsg": "user not in agent"}

    adapter = mod.WeComFullAdapter(config={})
    assert await adapter.connect() is True
    with patch("httpx.get", return_value=fresh_token), \
         patch("httpx.post", return_value=bad_send):
        result = await adapter.send(chat_id="x", text="x")
    assert result.success is False
    assert "user not in agent" in (result.error or "")


@pytest.mark.asyncio
async def test_connect_fails_without_credentials(monkeypatch):
    monkeypatch.delenv("WECOM_CORP_ID", raising=False)
    monkeypatch.delenv("WECOM_AGENT_ID", raising=False)
    monkeypatch.delenv("WECOM_SECRET", raising=False)
    mod = _load("adapter", _ADAPTER_PY)
    adapter = mod.WeComFullAdapter(config={})
    assert await adapter.connect() is False


@pytest.mark.asyncio
async def test_send_supports_party_recipient(monkeypatch):
    """WeCom send accepts touser / toparty / totag — kwargs route accordingly."""
    monkeypatch.setenv("WECOM_CORP_ID", "corpA")
    monkeypatch.setenv("WECOM_AGENT_ID", "1000001")
    monkeypatch.setenv("WECOM_SECRET", "secB")
    mod = _load("adapter", _ADAPTER_PY)

    captured = {}

    def fake_post(url, **kwargs):
        captured["body"] = kwargs.get("json")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"errcode": 0, "errmsg": "ok"}
        return resp

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"errcode": 0, "access_token": "x", "expires_in": 7200}

    adapter = mod.WeComFullAdapter(config={})
    assert await adapter.connect() is True
    with patch("httpx.get", return_value=fresh_token), \
         patch("httpx.post", side_effect=fake_post):
        await adapter.send(chat_id="party:42", text="ping")
    body = captured["body"]
    assert body.get("toparty") == "42"
    assert "touser" not in body or not body["touser"]


def test_plugin_manifest_exists():
    manifest_path = _REPO / "extensions" / "wecom" / "plugin.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kind"] == "channel"
    assert manifest["id"] == "wecom"  # distinct from wecom-callback
