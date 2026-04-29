"""Tests for chunker config wiring on Telegram/Discord/Slack adapters.

Verifies that each adapter reads ``config['streaming']`` block on init
and exposes the 5 chunker fields. The actual dispatch-side queue
serialization (per AMENDMENTS Fix C4) is a follow-up PR; this test
proves the per-adapter config plumbing is wired so dispatch can read
``adapter.streaming_block_chunker`` to detect opt-in.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
_TELEGRAM_ADAPTER_PY = _REPO / "extensions" / "telegram" / "adapter.py"
_DISCORD_ADAPTER_PY = _REPO / "extensions" / "discord" / "adapter.py"
_SLACK_ADAPTER_PY = _REPO / "extensions" / "slack" / "adapter.py"


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _telegram_cls():
    sys.modules.pop("adapter", None)
    return _load_module("telegram_adapter_test", _TELEGRAM_ADAPTER_PY).TelegramAdapter


def _discord_cls():
    sys.modules.pop("adapter", None)
    return _load_module("discord_adapter_test", _DISCORD_ADAPTER_PY).DiscordAdapter


def _slack_cls():
    sys.modules.pop("adapter", None)
    return _load_module("slack_adapter_test", _SLACK_ADAPTER_PY).SlackAdapter


# ── Defaults (chunker OFF when block missing) ──────────────────────────


def test_telegram_chunker_off_by_default():
    cls = _telegram_cls()
    a = cls(config={"bot_token": "test"})
    assert a.streaming_block_chunker is False


def test_discord_chunker_off_by_default():
    cls = _discord_cls()
    a = cls(config={"bot_token": "test"})
    assert a.streaming_block_chunker is False


def test_slack_chunker_off_by_default():
    cls = _slack_cls()
    a = cls(config={"bot_token": "test"})
    assert a.streaming_block_chunker is False


# ── Opt-in via streaming sub-block ─────────────────────────────────────


def test_telegram_chunker_reads_yaml_block():
    cls = _telegram_cls()
    a = cls(
        config={
            "bot_token": "test",
            "streaming": {
                "block_chunker": True,
                "min_chars": 120,
                "max_chars": 1000,
                "human_delay_min_ms": 1500,
                "human_delay_max_ms": 3000,
            },
        }
    )
    assert a.streaming_block_chunker is True
    assert a.streaming_min_chars == 120
    assert a.streaming_max_chars == 1000
    assert a.streaming_human_delay_min_ms == 1500
    assert a.streaming_human_delay_max_ms == 3000


def test_discord_chunker_reads_yaml_block():
    cls = _discord_cls()
    a = cls(
        config={"bot_token": "test", "streaming": {"block_chunker": True}}
    )
    assert a.streaming_block_chunker is True


def test_slack_chunker_reads_yaml_block():
    cls = _slack_cls()
    a = cls(
        config={"bot_token": "test", "streaming": {"block_chunker": True}}
    )
    assert a.streaming_block_chunker is True


# ── Platform-specific defaults (rate-limit floors) ─────────────────────


def test_telegram_default_floor_is_1000ms():
    cls = _telegram_cls()
    a = cls(config={"bot_token": "test", "streaming": {"block_chunker": True}})
    # Telegram per-chat send rate is ~1 msg/sec
    assert a.streaming_human_delay_min_ms == 1000


def test_discord_default_floor_is_1100ms():
    cls = _discord_cls()
    a = cls(config={"bot_token": "test", "streaming": {"block_chunker": True}})
    # Discord 5 msg/5 sec ≈ 1100ms minimum gap
    assert a.streaming_human_delay_min_ms == 1100


def test_slack_default_floor_is_1100ms():
    cls = _slack_cls()
    a = cls(config={"bot_token": "test", "streaming": {"block_chunker": True}})
    # Slack chat.postMessage tier ≈ 1 msg/sec
    assert a.streaming_human_delay_min_ms == 1100


# ── User can override floor (it's not a hard floor) ────────────────────


def test_user_can_override_human_delay_below_floor():
    """The defaults are platform-aware floors; users can still override
    if they know what they're doing (e.g., test environments)."""
    cls = _telegram_cls()
    a = cls(
        config={
            "bot_token": "test",
            "streaming": {
                "block_chunker": True,
                "human_delay_min_ms": 0,
                "human_delay_max_ms": 0,
            },
        }
    )
    assert a.streaming_human_delay_min_ms == 0
