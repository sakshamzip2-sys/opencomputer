"""Tests for opencomputer/gateway/delivery.py — DeliveryTarget + DeliveryRouter.

PR-2 Tasks B5 of the messaging-gateway parity plan. Mirrors Hermes
``gateway/delivery.py`` semantics without copying code.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.gateway.delivery import (
    LOCAL_PLATFORM,
    MAX_PLATFORM_OUTPUT,
    TRUNCATED_VISIBLE,
    DeliveryRouter,
    DeliveryTarget,
    SessionSource,
)
from plugin_sdk.core import Platform

# ── DeliveryTarget.parse ───────────────────────────────────────────────────


def test_parse_origin_with_session_source():
    """parse('origin', origin=...) → returns the source's platform/chat/thread."""
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="123", thread_id=None)
    target = DeliveryTarget.parse("origin", origin=src)
    assert target.platform == Platform.TELEGRAM
    assert target.chat_id == "123"
    assert target.is_origin is True


def test_parse_origin_without_source_falls_back_to_local():
    """parse('origin') with no origin returns LOCAL placeholder."""
    target = DeliveryTarget.parse("origin")
    assert target.platform == LOCAL_PLATFORM
    assert target.is_origin is True


def test_parse_local():
    """parse('local') → LOCAL_PLATFORM sentinel."""
    target = DeliveryTarget.parse("local")
    assert target.platform == LOCAL_PLATFORM
    assert target.chat_id is None
    assert target.is_origin is False


def test_parse_platform_only_means_home_channel():
    """parse('telegram') → telegram with chat_id=None (use home channel)."""
    target = DeliveryTarget.parse("telegram")
    assert target.platform == Platform.TELEGRAM
    assert target.chat_id is None
    assert target.is_explicit is False


def test_parse_platform_with_chat_id_is_explicit():
    """parse('telegram:123') → explicit chat id."""
    target = DeliveryTarget.parse("telegram:123")
    assert target.platform == Platform.TELEGRAM
    assert target.chat_id == "123"
    assert target.is_explicit is True
    assert target.thread_id is None


def test_parse_platform_with_chat_and_thread():
    """parse('telegram:123:T7') → thread_id set."""
    target = DeliveryTarget.parse("telegram:123:T7")
    assert target.platform == Platform.TELEGRAM
    assert target.chat_id == "123"
    assert target.thread_id == "T7"
    assert target.is_explicit is True


def test_parse_unknown_platform_falls_back_to_local():
    """Unknown platform like 'unknown:123' → LOCAL_PLATFORM."""
    target = DeliveryTarget.parse("unknown:123")
    assert target.platform == LOCAL_PLATFORM
    target2 = DeliveryTarget.parse("notreal")
    assert target2.platform == LOCAL_PLATFORM


def test_to_string_round_trip():
    """to_string() round-trips back to a parseable form."""
    cases = [
        "local",
        "telegram",
        "telegram:123",
        "telegram:123:T7",
        "origin",
    ]
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="9", thread_id=None)
    for s in cases:
        t = DeliveryTarget.parse(s, origin=src)
        # The round-trip should produce a target that, when re-parsed
        # with the same origin, yields equivalent fields.
        s2 = t.to_string()
        t2 = DeliveryTarget.parse(s2, origin=src)
        assert t.platform == t2.platform
        assert t.chat_id == t2.chat_id
        assert t.thread_id == t2.thread_id


# ── DeliveryRouter — truncation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_truncates_oversized_output(tmp_path, monkeypatch):
    """Text > MAX_PLATFORM_OUTPUT is truncated with a ``[truncated, full output saved to ...]`` suffix."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    captured: dict = {}

    async def fake_send(chat_id, text, **kwargs):
        captured["chat_id"] = chat_id
        captured["text"] = text
        return MagicMock(success=True, error=None)

    adapter = MagicMock()
    adapter.send = fake_send
    gateway = MagicMock(_adapters=[adapter])
    adapter.platform = Platform.TELEGRAM

    router = DeliveryRouter(gateway, mirror=False)
    target = DeliveryTarget.parse("telegram:99")
    big = "x" * (MAX_PLATFORM_OUTPUT + 500)
    results = await router.route(big, [target], source_label="cron")

    assert results[target.to_string()] is True
    sent = captured["text"]
    assert len(sent) > TRUNCATED_VISIBLE
    assert sent.startswith("x" * TRUNCATED_VISIBLE)
    assert "truncated" in sent
    assert "full output saved to" in sent


# ── DeliveryRouter.route ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_calls_adapter_send_for_each_target(tmp_path, monkeypatch):
    """Each non-local target hits adapter.send."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    sent_calls: list[tuple[str, str]] = []

    async def fake_send(chat_id, text, **kwargs):
        sent_calls.append((chat_id, text))
        return MagicMock(success=True, error=None)

    adapter = MagicMock()
    adapter.platform = Platform.TELEGRAM
    adapter.send = fake_send
    gateway = MagicMock(_adapters=[adapter])

    router = DeliveryRouter(gateway, mirror=False)
    targets = [
        DeliveryTarget.parse("telegram:1"),
        DeliveryTarget.parse("telegram:2:T7"),
    ]
    results = await router.route("hello", targets, source_label="cron")

    assert all(results.values())
    assert len(sent_calls) == 2
    chat_ids = {c[0] for c in sent_calls}
    assert chat_ids == {"1", "2"}


# ── DeliveryRouter — home channel resolution ───────────────────────────────


@pytest.mark.asyncio
async def test_home_channel_resolved_from_disk(tmp_path, monkeypatch):
    """Target with chat_id=None looks up <profile>/gateway/home_channels.json."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    home_dir = tmp_path / "gateway"
    home_dir.mkdir(parents=True)
    (home_dir / "home_channels.json").write_text(
        json.dumps({"telegram": "777"}), encoding="utf-8",
    )

    sent_calls: list[tuple[str, str]] = []

    async def fake_send(chat_id, text, **kwargs):
        sent_calls.append((chat_id, text))
        return MagicMock(success=True, error=None)

    adapter = MagicMock()
    adapter.platform = Platform.TELEGRAM
    adapter.send = fake_send
    gateway = MagicMock(_adapters=[adapter])

    router = DeliveryRouter(gateway, mirror=False)
    target = DeliveryTarget.parse("telegram")  # no chat_id
    results = await router.route("hi", [target], source_label="cron")

    assert results[target.to_string()] is True
    assert sent_calls == [("777", "hi")]


@pytest.mark.asyncio
async def test_missing_home_channel_returns_error(tmp_path, monkeypatch):
    """No home set + chat_id=None → delivery error (False)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    adapter = MagicMock()
    adapter.platform = Platform.TELEGRAM
    adapter.send = AsyncMock(return_value=MagicMock(success=True, error=None))
    gateway = MagicMock(_adapters=[adapter])

    router = DeliveryRouter(gateway, mirror=False)
    target = DeliveryTarget.parse("telegram")  # no home set on disk
    results = await router.route("hi", [target], source_label="cron")

    assert results[target.to_string()] is False
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_home_channel_with_thread(tmp_path, monkeypatch):
    """Home channel value 'CHATID:THREADID' is split into chat + thread."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    home_dir = tmp_path / "gateway"
    home_dir.mkdir(parents=True)
    (home_dir / "home_channels.json").write_text(
        json.dumps({"telegram": "5:T9"}), encoding="utf-8",
    )

    captured: dict = {}

    async def fake_send(chat_id, text, **kwargs):
        captured["chat_id"] = chat_id
        captured["kwargs"] = kwargs
        return MagicMock(success=True, error=None)

    adapter = MagicMock()
    adapter.platform = Platform.TELEGRAM
    adapter.send = fake_send
    gateway = MagicMock(_adapters=[adapter])

    router = DeliveryRouter(gateway, mirror=False)
    results = await router.route("hi", [DeliveryTarget.parse("telegram")])
    assert any(results.values())
    assert captured["chat_id"] == "5"
