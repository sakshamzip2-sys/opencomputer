"""Tests for the DingTalk channel adapter."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent


def _load_adapter():
    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location(
        "adapter", _REPO / "extensions" / "dingtalk" / "adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_class_attributes():
    mod = _load_adapter()
    from plugin_sdk.core import Platform
    assert mod.DingTalkAdapter.platform == Platform.DINGTALK


def test_sign_url_appends_timestamp_and_sign():
    mod = _load_adapter()
    signed = mod._sign_url("https://oapi.dingtalk.com/robot/send?access_token=xyz", "test-secret")
    assert "timestamp=" in signed
    assert "sign=" in signed
    assert signed.startswith("https://oapi.dingtalk.com/robot/send?access_token=xyz")


def test_init_reads_env_vars(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=x")
    monkeypatch.setenv("DINGTALK_SECRET", "my-secret")
    mod = _load_adapter()
    a = mod.DingTalkAdapter()
    assert "oapi.dingtalk.com" in a._webhook_url
    assert a._secret == "my-secret"


@pytest.mark.asyncio
async def test_connect_fails_without_url(monkeypatch):
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    mod = _load_adapter()
    a = mod.DingTalkAdapter()
    assert await a.connect() is False
    assert a.has_fatal_error()


@pytest.mark.asyncio
async def test_send_posts_markdown_payload(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=x")
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)
    mod = _load_adapter()

    posted: list[dict] = []

    class MockResp:
        status_code = 200
        text = ""
        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, **kwargs):
            posted.append({"url": url, "json": json})
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.DingTalkAdapter()
    await a.connect()
    result = await a.send("group-1", "**hello** dingtalk")
    assert result.success is True
    assert posted[0]["json"]["msgtype"] == "markdown"
    assert posted[0]["json"]["markdown"]["text"] == "**hello** dingtalk"


@pytest.mark.asyncio
async def test_send_signs_url_when_secret_set(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=x")
    monkeypatch.setenv("DINGTALK_SECRET", "my-secret")
    mod = _load_adapter()

    posted: list[dict] = []

    class MockResp:
        status_code = 200
        text = ""
        def json(self):
            return {"errcode": 0}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

        async def post(self, url, json=None, **kwargs):
            posted.append({"url": url, "json": json})
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.DingTalkAdapter()
    await a.connect()
    await a.send("c", "hi")
    # Signed URL should contain timestamp + sign params
    assert "timestamp=" in posted[0]["url"]
    assert "sign=" in posted[0]["url"]


@pytest.mark.asyncio
async def test_send_returns_failure_on_dingtalk_error(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=x")
    mod = _load_adapter()

    class MockResp:
        status_code = 200
        text = ""
        def json(self):
            return {"errcode": 310000, "errmsg": "keywords not in white list"}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, **kwargs):
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.DingTalkAdapter()
    await a.connect()
    result = await a.send("c", "hi")
    assert result.success is False
    assert "white list" in result.error


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "dingtalk" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    assert manifest["kind"] == "channel"
    setup = manifest["setup"]["channels"][0]
    assert setup["id"] == "dingtalk"
    assert "DINGTALK_WEBHOOK_URL" in setup["env_vars"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        _discover_platforms,
    )
    ids = {p["name"] for p in _discover_platforms()}
    assert "dingtalk" in ids
