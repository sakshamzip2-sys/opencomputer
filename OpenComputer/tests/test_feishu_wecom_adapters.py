"""Tests for Feishu + WeCom-Callback adapters (both webhook-based)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent


def _load(adapter_path: Path):
    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location("adapter", adapter_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Feishu ──

def test_feishu_class_attributes():
    mod = _load(_REPO / "extensions" / "feishu" / "adapter.py")
    from plugin_sdk.core import Platform
    assert mod.FeishuAdapter.platform == Platform.FEISHU


def test_feishu_sign_produces_base64():
    mod = _load(_REPO / "extensions" / "feishu" / "adapter.py")
    sig = mod._feishu_sign(1234567890, "test-secret")
    # Base64-encoded HMAC-SHA256 = 44 chars (32 bytes -> 44 chars b64)
    assert len(sig) == 44


@pytest.mark.asyncio
async def test_feishu_send_posts_text(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.delenv("FEISHU_SECRET", raising=False)
    mod = _load(_REPO / "extensions" / "feishu" / "adapter.py")

    posted: list[dict] = []

    class MockResp:
        status_code = 200
        text = ""
        def json(self):
            return {"code": 0, "msg": "ok"}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, **kwargs):
            posted.append({"url": url, "json": json})
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.FeishuAdapter()
    await a.connect()
    result = await a.send("c", "hi feishu")
    assert result.success is True
    assert posted[0]["json"]["msg_type"] == "text"
    assert posted[0]["json"]["content"]["text"] == "hi feishu"


@pytest.mark.asyncio
async def test_feishu_signs_when_secret_set(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/x")
    monkeypatch.setenv("FEISHU_SECRET", "my-secret")
    mod = _load(_REPO / "extensions" / "feishu" / "adapter.py")

    posted: list[dict] = []

    class MockResp:
        status_code = 200
        text = ""
        def json(self): return {"code": 0}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, **kwargs):
            posted.append({"json": json})
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.FeishuAdapter()
    await a.connect()
    await a.send("c", "hi")
    payload = posted[0]["json"]
    assert "timestamp" in payload
    assert "sign" in payload


def test_feishu_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "feishu" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["channels"][0]
    assert setup["id"] == "feishu"


# ── WeCom-Callback ──

def test_wecom_class_attributes():
    mod = _load(_REPO / "extensions" / "wecom-callback" / "adapter.py")
    from plugin_sdk.core import Platform
    assert mod.WeComCallbackAdapter.platform == Platform.WECOM


@pytest.mark.asyncio
async def test_wecom_send_posts_text(monkeypatch):
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x")
    mod = _load(_REPO / "extensions" / "wecom-callback" / "adapter.py")

    posted: list[dict] = []

    class MockResp:
        status_code = 200
        text = ""
        def json(self): return {"errcode": 0, "errmsg": "ok"}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, **kwargs):
            posted.append(json)
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.WeComCallbackAdapter()
    await a.connect()
    result = await a.send("c", "hi wecom")
    assert result.success is True
    assert posted[0]["msgtype"] == "text"
    assert posted[0]["text"]["content"] == "hi wecom"


@pytest.mark.asyncio
async def test_wecom_failure_on_errcode():
    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location(
        "adapter", _REPO / "extensions" / "wecom-callback" / "adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adapter"] = mod
    spec.loader.exec_module(mod)

    class MockResp:
        status_code = 200
        text = ""
        def json(self): return {"errcode": 93000, "errmsg": "invalid webhook"}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None, **kwargs):
            return MockResp()

    import unittest.mock as um
    with um.patch("httpx.AsyncClient", lambda **kw: MockClient()):
        a = mod.WeComCallbackAdapter({"webhook_url": "https://x"})
        await a.connect()
        result = await a.send("c", "hi")
        assert result.success is False
        assert "invalid webhook" in result.error


def test_wecom_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "wecom-callback" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    setup = manifest["setup"]["channels"][0]
    assert setup["id"] == "wecom-callback"


def test_both_appear_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        _discover_platforms,
    )
    ids = {p["name"] for p in _discover_platforms()}
    assert "feishu" in ids
    assert "wecom-callback" in ids
