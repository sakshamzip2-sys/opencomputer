"""Tests for opencomputer.evolution.rate_limit (Phase 5.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from opencomputer.evolution.rate_limit import DraftRateLimiter, RateLimitExceeded


@pytest.fixture
def rl(tmp_path):
    return DraftRateLimiter(
        db_path=tmp_path / "rate.db",
        per_day=1,
        lifetime=3,
    )


def test_allow_when_no_drafts(rl):
    rl.allow()  # no raise


def test_per_day_cap(rl):
    now = datetime.now(UTC)
    rl.record_draft(when=now)
    with pytest.raises(RateLimitExceeded, match="per-day"):
        rl.allow(now=now + timedelta(hours=1))


def test_per_day_window_rolls_off_after_24h(rl):
    now = datetime.now(UTC)
    rl.record_draft(when=now)
    # 25 hours later → window has rolled
    rl.allow(now=now + timedelta(hours=25))


def test_lifetime_cap(tmp_path):
    rl = DraftRateLimiter(db_path=tmp_path / "rate.db", per_day=999, lifetime=2)
    base = datetime.now(UTC)
    rl.record_draft(when=base + timedelta(days=1))
    rl.record_draft(when=base + timedelta(days=2))
    with pytest.raises(RateLimitExceeded, match="lifetime"):
        rl.allow(now=base + timedelta(days=10))


def test_reset_clears_all_counters(rl):
    now = datetime.now(UTC)
    rl.record_draft(when=now)
    rl.reset()
    rl.allow()  # no raise after reset


def test_db_persists_across_instances(tmp_path):
    p = tmp_path / "rate.db"
    a = DraftRateLimiter(db_path=p, per_day=1, lifetime=10)
    a.record_draft()
    b = DraftRateLimiter(db_path=p, per_day=1, lifetime=10)
    with pytest.raises(RateLimitExceeded):
        b.allow()


def test_default_path_under_user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    rl = DraftRateLimiter()
    assert str(rl.db_path).startswith(str(tmp_path))
