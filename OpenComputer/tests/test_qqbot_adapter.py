"""Tests for the QQ Bot Open Platform channel adapter.

QQ Bot has its own auth surface — appid + secret POSTed to bots.qq.com
returns a bot access_token (refreshable in 2 hours), used as
``QQBot {appid}.{access_token}`` in the Authorization header for messages
sent to api.sgroup.qq.com.
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
_TOKEN_PY = _REPO / "extensions" / "qqbot" / "token_cache.py"
_ADAPTER_PY = _REPO / "extensions" / "qqbot" / "adapter.py"


def _load(name: str, path: Path):
    sys.modules.pop(f"qqbot_test_{name}", None)
    spec = importlib.util.spec_from_file_location(f"qqbot_test_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"qqbot_test_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# token_cache.py
# --------------------------------------------------------------------------- #

def test_fetch_bot_token_posts_json_body():
    """Unlike Weixin, QQ Bot's token endpoint takes a JSON body."""
    mod = _load("token", _TOKEN_PY)
    fresh = MagicMock()
    fresh.status_code = 200
    fresh.json.return_value = {"access_token": "qq-tok", "expires_in": 7200}

    with patch("httpx.post", return_value=fresh) as called:
        token, expires_at = mod.fetch_bot_token(appid="qq-app", secret="qq-sec")

    assert token == "qq-tok"
    assert expires_at > time.time() + 7000
    args, kwargs = called.call_args
    assert "bots.qq.com/app/getAppAccessToken" in args[0]
    body = kwargs.get("json", {})
    assert body["appId"] == "qq-app"
    assert body["clientSecret"] == "qq-sec"


def test_fetch_bot_token_raises_on_non_200():
    mod = _load("token", _TOKEN_PY)
    bad = MagicMock()
    bad.status_code = 401
    bad.text = '{"code":401,"message":"unauthorized"}'
    with patch("httpx.post", return_value=bad):
        with pytest.raises(RuntimeError, match="401"):
            mod.fetch_bot_token(appid="bad", secret="x")


def test_token_cache_returns_cached():
    mod = _load("token", _TOKEN_PY)
    cache = mod.QQBotTokenCache(appid="a", secret="b")
    cache._access_token = "cached"
    cache._expires_at = time.time() + 3600
    with patch("httpx.post") as called:
        token = cache.get_access_token()
    assert token == "cached"
    called.assert_not_called()


def test_token_cache_refreshes_on_skew():
    mod = _load("token", _TOKEN_PY)
    cache = mod.QQBotTokenCache(appid="a", secret="b")
    cache._access_token = "stale"
    cache._expires_at = time.time() + 30  # within skew
    fresh = MagicMock()
    fresh.status_code = 200
    fresh.json.return_value = {"access_token": "fresh", "expires_in": 7200}
    with patch("httpx.post", return_value=fresh):
        token = cache.get_access_token()
    assert token == "fresh"


# --------------------------------------------------------------------------- #
# Adapter — outbound send
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_send_text_to_group(monkeypatch):
    monkeypatch.setenv("QQBOT_APPID", "qq-app")
    monkeypatch.setenv("QQBOT_SECRET", "qq-sec")
    mod = _load("adapter", _ADAPTER_PY)

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["body"] = kwargs.get("json")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "MSG-1", "timestamp": 1700000000}
        return resp

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"access_token": "qq-tok", "expires_in": 7200}

    adapter = mod.QQBotAdapter(config={})
    assert await adapter.connect() is True
    with patch("httpx.post", side_effect=[fresh_token, fake_post("https://x", json={"x":1})]):
        # Reset the side effect properly — we want first call to token endpoint
        # then second to message endpoint
        pass

    # Re-run with the real flow
    with patch("httpx.post") as posted:
        # First call → token, second call → message send
        posted.side_effect = [fresh_token, MagicMock(
            status_code=200, json=lambda: {"id": "MSG-1"}
        )]
        # Capture what's sent on the second call
        original_side_effect = posted.side_effect
        captured_calls = []
        def capturing_post(url, **kw):
            captured_calls.append((url, kw))
            if len(captured_calls) == 1:
                return fresh_token
            else:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"id": "MSG-1", "timestamp": 1700000000}
                return resp
        posted.side_effect = capturing_post
        result = await adapter.send(chat_id="group_openid_123", text="hello")

    assert result.success is True
    assert result.message_id == "MSG-1"
    # First captured call is the token POST; second is the message
    assert len(captured_calls) == 2
    msg_url, msg_kw = captured_calls[1]
    assert "/v2/groups/group_openid_123/messages" in msg_url
    auth = msg_kw["headers"]["Authorization"]
    assert auth.startswith("QQBot ")
    assert msg_kw["json"]["content"] == "hello"
    assert msg_kw["json"]["msg_type"] == 0  # 0 = text


@pytest.mark.asyncio
async def test_send_supports_dm_recipient(monkeypatch):
    """``user:openid`` prefix routes to /v2/users/{id}/messages."""
    monkeypatch.setenv("QQBOT_APPID", "qq-app")
    monkeypatch.setenv("QQBOT_SECRET", "qq-sec")
    mod = _load("adapter", _ADAPTER_PY)

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"access_token": "tok", "expires_in": 7200}
    msg_resp = MagicMock()
    msg_resp.status_code = 200
    msg_resp.json.return_value = {"id": "MSG-2"}

    captured = []

    def capturing(url, **kw):
        captured.append((url, kw))
        return fresh_token if len(captured) == 1 else msg_resp

    adapter = mod.QQBotAdapter(config={})
    assert await adapter.connect() is True
    with patch("httpx.post", side_effect=capturing):
        result = await adapter.send(chat_id="user:user-openid-99", text="hi")
    assert result.success is True
    assert "/v2/users/user-openid-99/messages" in captured[1][0]


@pytest.mark.asyncio
async def test_connect_fails_without_credentials(monkeypatch):
    monkeypatch.delenv("QQBOT_APPID", raising=False)
    monkeypatch.delenv("QQBOT_SECRET", raising=False)
    mod = _load("adapter", _ADAPTER_PY)
    adapter = mod.QQBotAdapter(config={})
    assert await adapter.connect() is False


@pytest.mark.asyncio
async def test_send_propagates_api_error(monkeypatch):
    monkeypatch.setenv("QQBOT_APPID", "a")
    monkeypatch.setenv("QQBOT_SECRET", "b")
    mod = _load("adapter", _ADAPTER_PY)

    fresh_token = MagicMock()
    fresh_token.status_code = 200
    fresh_token.json.return_value = {"access_token": "x", "expires_in": 7200}
    bad_send = MagicMock()
    bad_send.status_code = 400
    bad_send.text = '{"code":40034,"message":"send failed"}'

    captured = []

    def capturing(url, **kw):
        captured.append(url)
        return fresh_token if len(captured) == 1 else bad_send

    adapter = mod.QQBotAdapter(config={})
    assert await adapter.connect() is True
    with patch("httpx.post", side_effect=capturing):
        result = await adapter.send(chat_id="g-1", text="x")
    assert result.success is False
    assert "send failed" in (result.error or "")


def test_plugin_manifest():
    manifest_path = _REPO / "extensions" / "qqbot" / "plugin.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["kind"] == "channel"
    setup = manifest["setup"]["channels"][0]
    assert "QQBOT_APPID" in setup["env_vars"]


def test_platform_qqbot_added():
    from plugin_sdk.core import Platform
    assert hasattr(Platform, "QQBOT")
