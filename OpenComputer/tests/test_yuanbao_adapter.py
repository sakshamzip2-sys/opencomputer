"""Tests for the Tencent Yuanbao channel adapter."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent


def _load():
    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location(
        "adapter", _REPO / "extensions" / "yuanbao" / "adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_class_attributes():
    mod = _load()
    from plugin_sdk.core import Platform
    assert mod.YuanbaoAdapter.platform == Platform.YUANBAO


def test_init_reads_env_vars(monkeypatch):
    monkeypatch.setenv("YUANBAO_WEBHOOK_URL", "https://yuanbao.tencent.com/webhook/abc")
    monkeypatch.setenv("YUANBAO_API_KEY", "yuanbao-key")
    mod = _load()
    a = mod.YuanbaoAdapter()
    assert "yuanbao.tencent.com" in a._webhook_url
    assert a._api_key == "yuanbao-key"


@pytest.mark.asyncio
async def test_connect_fails_without_url(monkeypatch):
    monkeypatch.delenv("YUANBAO_WEBHOOK_URL", raising=False)
    mod = _load()
    a = mod.YuanbaoAdapter()
    assert await a.connect() is False
    assert a.has_fatal_error()


@pytest.mark.asyncio
async def test_send_posts_with_bearer_when_key_set(monkeypatch):
    monkeypatch.setenv("YUANBAO_WEBHOOK_URL", "https://yuanbao.tencent.com/x")
    monkeypatch.setenv("YUANBAO_API_KEY", "yuanbao-key")
    mod = _load()

    posted: list[dict] = []

    class MockResp:
        status_code = 200
        text = ""

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, headers=None, **kwargs):
            posted.append({"json": json, "headers": headers})
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.YuanbaoAdapter()
    await a.connect()
    result = await a.send("user-1", "hello yuanbao")
    assert result.success is True
    assert posted[0]["json"]["content"] == "hello yuanbao"
    assert posted[0]["headers"]["Authorization"] == "Bearer yuanbao-key"


@pytest.mark.asyncio
async def test_send_omits_auth_header_when_no_key(monkeypatch):
    monkeypatch.setenv("YUANBAO_WEBHOOK_URL", "https://yuanbao.tencent.com/x")
    monkeypatch.delenv("YUANBAO_API_KEY", raising=False)
    mod = _load()

    posted: list[dict] = []

    class MockResp:
        status_code = 200
        text = ""

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, headers=None, **kwargs):
            posted.append({"headers": headers or {}})
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.YuanbaoAdapter()
    await a.connect()
    await a.send("c", "hi")
    assert "Authorization" not in posted[0]["headers"]


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "yuanbao" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    assert manifest["kind"] == "channel"
    setup = manifest["setup"]["channels"][0]
    assert setup["id"] == "yuanbao"


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        _discover_platforms,
    )
    ids = {p["name"] for p in _discover_platforms()}
    assert "yuanbao" in ids
