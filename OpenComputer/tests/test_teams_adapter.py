"""Tests for the Microsoft Teams channel adapter."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent


def _load_adapter():
    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location(
        "adapter", _REPO / "extensions" / "teams" / "adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_class_attributes():
    mod = _load_adapter()
    from plugin_sdk.core import Platform
    assert mod.TeamsAdapter.platform == Platform.TEAMS


def test_init_reads_env_var(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://outlook.office.com/webhook/abc")
    mod = _load_adapter()
    a = mod.TeamsAdapter()
    assert a._webhook_url == "https://outlook.office.com/webhook/abc"


def test_init_falls_back_to_empty_when_unset(monkeypatch):
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    mod = _load_adapter()
    a = mod.TeamsAdapter()
    assert a._webhook_url == ""


@pytest.mark.asyncio
async def test_connect_fails_when_url_missing(monkeypatch):
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    mod = _load_adapter()
    a = mod.TeamsAdapter()
    success = await a.connect()
    assert success is False
    assert a.has_fatal_error()


@pytest.mark.asyncio
async def test_connect_succeeds_with_url(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://outlook.office.com/webhook/x")
    mod = _load_adapter()
    a = mod.TeamsAdapter()
    success = await a.connect()
    assert success is True


@pytest.mark.asyncio
async def test_send_returns_failure_when_not_connected():
    mod = _load_adapter()
    a = mod.TeamsAdapter({"webhook_url": "https://x"})
    # Not connected yet
    result = await a.send("any-chat", "hi")
    assert result.success is False


@pytest.mark.asyncio
async def test_send_posts_adaptive_card_to_webhook(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://outlook.office.com/webhook/test")
    mod = _load_adapter()

    posted_payloads: list[dict] = []

    class MockResp:
        status_code = 200
        text = "ok"

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, **kwargs):
            posted_payloads.append({"url": url, "json": json})
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.TeamsAdapter()
    await a.connect()
    result = await a.send("ignored", "hello team")

    assert result.success is True
    assert len(posted_payloads) == 1
    payload = posted_payloads[0]["json"]
    assert payload["type"] == "message"
    body = payload["attachments"][0]["content"]["body"][0]
    assert body["text"] == "hello team"


@pytest.mark.asyncio
async def test_send_returns_failure_on_4xx(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://outlook.office.com/webhook/x")
    mod = _load_adapter()

    class MockResp:
        status_code = 400
        text = "bad webhook"

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, **kwargs):
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.TeamsAdapter()
    await a.connect()
    result = await a.send("c", "hi")
    assert result.success is False
    assert "400" in result.error


@pytest.mark.asyncio
async def test_send_truncates_overly_long_text(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://x")
    mod = _load_adapter()

    posted_payloads: list[dict] = []

    class MockResp:
        status_code = 200
        text = "ok"

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, **kwargs):
            posted_payloads.append(json)
            return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: MockClient())

    a = mod.TeamsAdapter()
    await a.connect()
    huge = "x" * 50_000
    await a.send("c", huge)
    body = posted_payloads[0]["attachments"][0]["content"]["body"][0]
    assert len(body["text"]) <= a.max_message_length
    assert "truncated" in body["text"]


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "teams" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    assert manifest["kind"] == "channel"
    setup = manifest["setup"]["channels"][0]
    assert setup["id"] == "teams"
    assert setup["env_vars"] == ["TEAMS_WEBHOOK_URL"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        _discover_platforms,
    )
    ids = {p["name"] for p in _discover_platforms()}
    assert "teams" in ids
