"""TS-T7 — Cross-session rate-limit guard tests.

Mirrors the test plan in
``docs/superpowers/plans/2026-04-27-tier-s-port.md`` Task 7. Uses the
``OPENCOMPUTER_HOME`` env-var to redirect the per-profile state path
into ``tmp_path`` so tests don't touch ``~/.opencomputer``.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from opencomputer.agent.rate_guard import (
    _state_path,
    clear_rate_limit,
    format_remaining,
    rate_limit_remaining,
    record_rate_limit,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Redirect ``_home()`` into a fresh tmp dir per test."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))


def test_no_rate_limit_returns_none() -> None:
    assert rate_limit_remaining("anthropic") is None


def test_record_then_check_returns_remaining() -> None:
    record_rate_limit("anthropic", default_cooldown=60.0)
    remaining = rate_limit_remaining("anthropic")
    assert remaining is not None
    # Should be roughly 60s, allow a small slack for runtime.
    assert 50 <= remaining <= 60


def test_separate_providers_isolated() -> None:
    record_rate_limit("anthropic", default_cooldown=60.0)
    # The OpenAI state was never written, so it must remain unset.
    assert rate_limit_remaining("openai") is None
    # Anthropic's state still reads back.
    assert rate_limit_remaining("anthropic") is not None

    # And recording one provider's state doesn't disturb the other.
    record_rate_limit("openai", default_cooldown=120.0)
    a = rate_limit_remaining("anthropic")
    o = rate_limit_remaining("openai")
    assert a is not None and o is not None
    # OpenAI cooldown is roughly twice Anthropic's.
    assert o > a


def test_expired_state_returns_none(tmp_path) -> None:
    # Pre-write an already-expired state file directly to disk.
    path = _state_path("anthropic")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            {
                "provider": "anthropic",
                "reset_at": time.time() - 10.0,
                "recorded_at": time.time() - 70.0,
                "reset_seconds": 60.0,
            },
            f,
        )
    assert rate_limit_remaining("anthropic") is None
    # And expired states are cleaned up so subsequent calls don't keep
    # paying the JSON parse cost.
    assert not os.path.exists(path)


def test_clear_removes_state() -> None:
    record_rate_limit("anthropic", default_cooldown=60.0)
    assert rate_limit_remaining("anthropic") is not None
    clear_rate_limit("anthropic")
    assert rate_limit_remaining("anthropic") is None
    # Idempotent — clearing an already-cleared state must not raise.
    clear_rate_limit("anthropic")


def test_record_uses_retry_after_header() -> None:
    record_rate_limit("anthropic", headers={"retry-after": "42"})
    remaining = rate_limit_remaining("anthropic")
    assert remaining is not None
    assert 32 <= remaining <= 42


def test_record_uses_x_ratelimit_reset_header() -> None:
    # The 1h header has the highest priority — even when retry-after is
    # also present and shorter, the 1h header wins.
    record_rate_limit(
        "anthropic",
        headers={
            "retry-after": "5",
            "x-ratelimit-reset-requests": "60",
            "x-ratelimit-reset-requests-1h": "3600",
        },
    )
    remaining = rate_limit_remaining("anthropic")
    assert remaining is not None
    # ~3600s, with a small slack for runtime.
    assert 3500 <= remaining <= 3600


def test_format_remaining_minutes() -> None:
    # < 60s = seconds form.
    assert format_remaining(0) == "0s"
    assert format_remaining(45) == "45s"
    # exactly minute boundary.
    assert format_remaining(60) == "1m"
    # mixed minutes + seconds.
    assert format_remaining(125) == "2m 5s"
    # Large minute count (still < 1h).
    assert format_remaining(3599) == "59m 59s"


def test_format_remaining_hours() -> None:
    # exactly hour boundary.
    assert format_remaining(3600) == "1h"
    # mixed hours + minutes.
    assert format_remaining(3600 + 30 * 60) == "1h 30m"
    # negative -> clamped to 0.
    assert format_remaining(-5) == "0s"


def test_atomic_write_handles_corrupt_state() -> None:
    # Write garbage to the state file directly — the reader must
    # transparently return None instead of propagating the JSON error.
    path = _state_path("anthropic")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")

    # Corrupt state -> read returns None.
    assert rate_limit_remaining("anthropic") is None

    # And a subsequent record_rate_limit replaces it cleanly via the
    # tempfile + os.replace path (no leftover .tmp partial).
    record_rate_limit("anthropic", default_cooldown=60.0)
    assert rate_limit_remaining("anthropic") is not None

    state_dir = os.path.dirname(path)
    leftover_tmp = [n for n in os.listdir(state_dir) if n.endswith(".tmp")]
    assert leftover_tmp == []
