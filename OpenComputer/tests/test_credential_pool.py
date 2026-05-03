"""PR-A: unit tests for opencomputer.agent.credential_pool.

Covers pool construction, least-used distribution, quarantine, expiry,
retry logic, concurrency, and stats output.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from opencomputer.agent.credential_pool import (
    ROTATE_COOLDOWN_SECONDS,
    STRATEGY_FILL_FIRST,
    STRATEGY_LEAST_USED,
    STRATEGY_RANDOM,
    STRATEGY_ROUND_ROBIN,
    CredentialPool,
    CredentialPoolExhausted,
)

# ─── construction ────────────────────────────────────────────────────────────


def test_constructor_rejects_empty_keys():
    with pytest.raises(ValueError, match="at least one key"):
        CredentialPool(keys=[])


def test_size_reflects_keys():
    pool = CredentialPool(keys=["a", "b", "c"])
    assert pool.size == 3


# ─── single-key regression ───────────────────────────────────────────────────


async def test_single_key_returns_same_key_every_acquire():
    """REGRESSION: pool-of-1 always returns the same key (identical to no-pool path)."""
    pool = CredentialPool(keys=["only-key"])
    for _ in range(5):
        key = await pool.acquire()
        assert key == "only-key"


# ─── distribution ─────────────────────────────────────────────────────────────


async def test_least_used_distribution():
    """3 keys × 9 acquires → each used exactly 3 times."""
    pool = CredentialPool(keys=["k1", "k2", "k3"])
    counts: dict[str, int] = {}
    for _ in range(9):
        key = await pool.acquire()
        counts[key] = counts.get(key, 0) + 1
    assert counts == {"k1": 3, "k2": 3, "k3": 3}


# ─── quarantine ───────────────────────────────────────────────────────────────


async def test_quarantine_skips_failed_key():
    pool = CredentialPool(keys=["bad", "good"], rotate_cooldown_seconds=9999.0)
    await pool.report_auth_failure("bad", reason="401")
    # Only "good" should come back now
    for _ in range(3):
        key = await pool.acquire()
        assert key == "good"


async def test_all_quarantined_raises_exhausted():
    pool = CredentialPool(keys=["a", "b"], rotate_cooldown_seconds=9999.0)
    await pool.report_auth_failure("a", reason="401")
    await pool.report_auth_failure("b", reason="401")
    with pytest.raises(CredentialPoolExhausted):
        await pool.acquire()


# ─── quarantine expiry ────────────────────────────────────────────────────────


async def test_quarantine_expires_after_cooldown(monkeypatch):
    """Monkeypatch time.time to simulate cooldown expiry."""
    import time as time_mod

    base_time = 1_000_000.0
    current_time = [base_time]

    monkeypatch.setattr(time_mod, "time", lambda: current_time[0])

    pool = CredentialPool(keys=["k1"], rotate_cooldown_seconds=60.0)
    await pool.report_auth_failure("k1", reason="test")

    # Still quarantined
    with pytest.raises(CredentialPoolExhausted):
        await pool.acquire()

    # Advance time past cooldown
    current_time[0] = base_time + 61.0

    key = await pool.acquire()
    assert key == "k1"


# ─── with_retry ───────────────────────────────────────────────────────────────


async def test_with_retry_rotates_on_auth_failure():
    """Bad key fails with auth error; pool rotates to good key and succeeds."""
    pool = CredentialPool(keys=["bad-key", "good-key"], rotate_cooldown_seconds=60.0)
    call_log: list[str] = []

    async def fn(key: str) -> str:
        call_log.append(key)
        if key == "bad-key":
            raise RuntimeError("401 Unauthorized")
        return "ok"

    result = await pool.with_retry(fn, is_auth_failure=lambda e: "401" in str(e))
    assert result == "ok"
    assert "bad-key" in call_log
    assert "good-key" in call_log


async def test_with_retry_propagates_non_auth_errors():
    """Non-auth exception is re-raised immediately without rotation."""
    pool = CredentialPool(keys=["k1", "k2"])
    call_log: list[str] = []

    async def fn(key: str):
        call_log.append(key)
        raise ValueError("network error")

    with pytest.raises(ValueError, match="network error"):
        await pool.with_retry(fn, is_auth_failure=lambda e: "401" in str(e))

    # Only called once — no rotation for non-auth error
    assert len(call_log) == 1


async def test_with_retry_exhausts_after_max_attempts():
    """All keys fail auth → CredentialPoolExhausted after max_rotation_attempts."""
    pool = CredentialPool(
        keys=["k1", "k2", "k3"],
        max_rotation_attempts=3,
        rotate_cooldown_seconds=9999.0,
    )

    async def fn(key: str):
        raise RuntimeError("401")

    with pytest.raises(CredentialPoolExhausted):
        await pool.with_retry(fn, is_auth_failure=lambda e: "401" in str(e))


# ─── concurrency ──────────────────────────────────────────────────────────────


async def test_concurrent_acquires_serialize_correctly():
    """50 async tasks each acquire once — counts add up and distribution is even."""
    n_tasks = 50
    n_keys = 5
    pool = CredentialPool(keys=[f"key-{i}" for i in range(n_keys)])

    results: list[str] = []

    async def acquire_one():
        results.append(await pool.acquire())

    await asyncio.gather(*[acquire_one() for _ in range(n_tasks)])

    assert len(results) == n_tasks
    counts = {k: results.count(k) for k in set(results)}
    # Each key should be used ~10 times; allow ±3 slack
    expected = n_tasks // n_keys
    for k, c in counts.items():
        assert abs(c - expected) <= 3, f"{k} used {c} times, expected ~{expected}"


# ─── stats ────────────────────────────────────────────────────────────────────


async def test_stats_returns_diagnostic_dict():
    """stats() returns a dict with size + per-key info; full key is not exposed."""
    pool = CredentialPool(keys=["sk-verylongkey1234567890", "sk-another"])
    await pool.acquire()

    s = pool.stats()
    assert s["size"] == 2
    assert len(s["keys"]) == 2

    first = s["keys"][0]
    # key_preview must be truncated — never the full key
    assert "..." in first["key_preview"]
    assert "sk-verylo" not in first["key_preview"]  # not the full key
    assert "use_count" in first
    assert "quarantined" in first
    assert "quarantine_remaining_s" in first


# ─── strategy selection ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fill_first_always_picks_first_eligible():
    pool = CredentialPool(keys=["k1", "k2", "k3"], strategy=STRATEGY_FILL_FIRST)
    assert await pool.acquire() == "k1"
    assert await pool.acquire() == "k1"
    assert await pool.acquire() == "k1"


@pytest.mark.asyncio
async def test_round_robin_cycles_keys():
    pool = CredentialPool(keys=["k1", "k2", "k3"], strategy=STRATEGY_ROUND_ROBIN)
    assert await pool.acquire() == "k1"
    assert await pool.acquire() == "k2"
    assert await pool.acquire() == "k3"
    assert await pool.acquire() == "k1"


@pytest.mark.asyncio
async def test_random_strategy_returns_valid_key():
    pool = CredentialPool(keys=["k1", "k2"], strategy=STRATEGY_RANDOM)
    for _ in range(10):
        key = await pool.acquire()
        assert key in ("k1", "k2")


@pytest.mark.asyncio
async def test_least_used_default_unchanged():
    pool = CredentialPool(keys=["k1", "k2"])  # default = STRATEGY_LEAST_USED
    k = await pool.acquire()
    assert k in ("k1", "k2")


@pytest.mark.asyncio
async def test_invalid_strategy_raises():
    with pytest.raises(ValueError, match="strategy must be one of"):
        CredentialPool(keys=["k"], strategy="bogus")


@pytest.mark.asyncio
async def test_single_key_still_works_with_all_strategies():
    """Regression: single-key pool IDENTICAL to no-pool path."""
    for s in (STRATEGY_FILL_FIRST, STRATEGY_ROUND_ROBIN, STRATEGY_RANDOM, STRATEGY_LEAST_USED):
        pool = CredentialPool(keys=["only-key"], strategy=s)
        assert await pool.acquire() == "only-key"


# ─── reset_at + JWT refresh ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_at_overrides_flat_cooldown():
    pool = CredentialPool(keys=["k1", "k2"])
    future = time.time() + 9999
    await pool.report_auth_failure("k1", reason="429", reset_at=future)
    stats = pool.stats()
    k1_stat = next(s for s in stats["keys"] if s["key_preview"].startswith("k1"))
    assert k1_stat["quarantine_remaining_s"] > 9990


@pytest.mark.asyncio
async def test_reset_at_in_past_uses_default_ttl():
    pool = CredentialPool(keys=["k1", "k2"])
    past = time.time() - 100
    await pool.report_auth_failure("k1", reason="429", reset_at=past)
    stats = pool.stats()
    k1_stat = next(s for s in stats["keys"] if s["key_preview"].startswith("k1"))
    # Should use default cooldown of 60s, not a past reset_at
    assert k1_stat["quarantine_remaining_s"] <= ROTATE_COOLDOWN_SECONDS


@pytest.mark.asyncio
async def test_jwt_refresh_called_before_expiry():
    """Pool calls refresher() when JWT token is within 60s of expiry."""
    import base64
    import json

    def make_jwt(exp: float) -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": int(exp)}).encode()
        ).rstrip(b"=").decode()
        return f"{header}.{payload}.sig"

    refreshed: list[str] = []

    async def mock_refresher(old_key: str) -> str:
        refreshed.append(old_key)
        return "new-token"

    jwt = make_jwt(time.time() + 30)  # expires in 30s — within 60s threshold
    pool = CredentialPool(keys=[jwt], refresher=mock_refresher)
    key = await pool.acquire()
    assert key == "new-token"
    assert len(refreshed) == 1


@pytest.mark.asyncio
async def test_non_jwt_key_skips_refresh():
    """Non-JWT key (not 3-part base64url) never calls refresher."""
    refreshed: list[str] = []

    async def mock_refresher(old_key: str) -> str:
        refreshed.append(old_key)
        return "refreshed"

    pool = CredentialPool(keys=["plain-api-key"], refresher=mock_refresher)
    key = await pool.acquire()
    assert key == "plain-api-key"
    assert len(refreshed) == 0
