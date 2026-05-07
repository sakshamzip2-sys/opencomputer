"""Tests for per-platform session reset policies (Task 1.3).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (Task 1.3)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.3)

Reset modes:
- ``off``    — never reset
- ``idle``   — reset when (now - last_seen) >= idle_minutes * 60
- ``daily``  — reset when ``now`` crosses ``daily_at_hour`` boundary since last_seen
- ``both``   — reset on either condition (default)

Per-platform overrides via ``ResetPolicyConfig.by_platform``.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from opencomputer.gateway.reset_policy import (
    ResetPolicy,
    ResetPolicyChecker,
    ResetPolicyConfig,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    """Return a stable POSIX timestamp for tests (UTC, ignores DST)."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


# ── Mode: off ──────────────────────────────────────────────────────────────


def test_mode_off_never_resets():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="off"))
    checker = ResetPolicyChecker(cfg, now_fn=lambda: _at(2026, 5, 8, 12))
    do, reason = checker.should_reset("telegram", "chat1", _at(2024, 1, 1))
    assert do is False
    assert reason == "off"


# ── Mode: idle ─────────────────────────────────────────────────────────────


def test_mode_idle_resets_when_threshold_exceeded():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="idle", idle_minutes=60))
    now = _at(2026, 5, 8, 12)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    # last_seen 2 hours ago — exceeds 60-minute idle.
    do, reason = checker.should_reset("telegram", "chat1", now - 7200)
    assert do is True
    assert reason.startswith("idle:")


def test_mode_idle_does_not_reset_below_threshold():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="idle", idle_minutes=60))
    now = _at(2026, 5, 8, 12)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    # last_seen 30 minutes ago — within 60-minute idle.
    do, reason = checker.should_reset("telegram", "chat1", now - 1800)
    assert do is False


def test_mode_idle_zero_lastseen_resets():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="idle", idle_minutes=60))
    checker = ResetPolicyChecker(cfg, now_fn=lambda: _at(2026, 5, 8, 12))
    do, _ = checker.should_reset("telegram", "chat1", 0.0)
    assert do is True


# ── Mode: daily ────────────────────────────────────────────────────────────


def test_mode_daily_resets_after_boundary_crossing():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="daily", daily_at_hour=4))
    # last_seen yesterday 11pm; now = 5am today (crossed today's 4am boundary).
    last = _at(2026, 5, 7, 23)
    now = _at(2026, 5, 8, 5)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    do, reason = checker.should_reset("telegram", "chat1", last)
    assert do is True
    assert reason.startswith("daily:")


def test_mode_daily_no_reset_within_same_window():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="daily", daily_at_hour=4))
    # both timestamps after today's 4am — same daily window.
    last = _at(2026, 5, 8, 5)
    now = _at(2026, 5, 8, 23)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    do, _ = checker.should_reset("telegram", "chat1", last)
    assert do is False


def test_mode_daily_yesterday_still_no_reset_before_boundary():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="daily", daily_at_hour=4))
    # last_seen 2am today; now = 3am today (boundary not crossed yet).
    last = _at(2026, 5, 8, 2)
    now = _at(2026, 5, 8, 3)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    do, _ = checker.should_reset("telegram", "chat1", last)
    assert do is False


# ── Mode: both ─────────────────────────────────────────────────────────────


def test_mode_both_idle_path():
    cfg = ResetPolicyConfig(
        default=ResetPolicy(mode="both", idle_minutes=60, daily_at_hour=4)
    )
    now = _at(2026, 5, 8, 12)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    # Same daily window but idle exceeds.
    do, reason = checker.should_reset("telegram", "chat1", now - 7200)
    assert do is True
    assert reason.startswith("idle:")


def test_mode_both_daily_path():
    cfg = ResetPolicyConfig(
        default=ResetPolicy(mode="both", idle_minutes=600, daily_at_hour=4)
    )
    last = _at(2026, 5, 7, 23)
    now = _at(2026, 5, 8, 5)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    # Within idle (6 hours < 10), but boundary crossed → daily fires.
    do, reason = checker.should_reset("telegram", "chat1", last)
    assert do is True
    assert reason.startswith("daily:")


def test_mode_both_no_reset_when_both_clean():
    cfg = ResetPolicyConfig(
        default=ResetPolicy(mode="both", idle_minutes=60, daily_at_hour=4)
    )
    last = _at(2026, 5, 8, 5)
    now = _at(2026, 5, 8, 5, 30)  # 30 min later, same daily window
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    do, _ = checker.should_reset("telegram", "chat1", last)
    assert do is False


# ── Per-platform overrides ─────────────────────────────────────────────────


def test_per_platform_override_resolved():
    cfg = ResetPolicyConfig(
        default=ResetPolicy(mode="both", idle_minutes=1440),
        by_platform={
            "telegram": ResetPolicy(mode="idle", idle_minutes=240),
        },
    )
    now = _at(2026, 5, 8, 12)
    checker = ResetPolicyChecker(cfg, now_fn=lambda: now)
    # Telegram override: 4 hours threshold — last_seen 5h ago triggers.
    do_tg, _ = checker.should_reset("telegram", "chat1", now - 18000)
    do_dc, _ = checker.should_reset("discord", "chat1", now - 18000)
    assert do_tg is True   # Telegram's tighter threshold
    # Discord uses default 1440 minutes (24h); 5h doesn't trigger.
    assert do_dc is False


def test_policy_for_platform_returns_override():
    cfg = ResetPolicyConfig(
        default=ResetPolicy(mode="both", idle_minutes=1440),
        by_platform={"telegram": ResetPolicy(mode="off")},
    )
    checker = ResetPolicyChecker(cfg, now_fn=lambda: _at(2026, 5, 8))
    assert checker.policy_for("telegram").mode == "off"
    assert checker.policy_for("discord").mode == "both"


def test_policy_for_unknown_platform_returns_default():
    cfg = ResetPolicyConfig(default=ResetPolicy(mode="idle"))
    checker = ResetPolicyChecker(cfg, now_fn=lambda: _at(2026, 5, 8))
    assert checker.policy_for("nonexistent").mode == "idle"


def test_should_reset_returns_tuple():
    cfg = ResetPolicyConfig()
    checker = ResetPolicyChecker(cfg, now_fn=lambda: _at(2026, 5, 8))
    result = checker.should_reset("telegram", "chat1", 0.0)
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], bool)
    assert isinstance(result[1], str)
