"""Tests for extensions/opencli-scraper/rate_limiter.py.

Tests cover token-bucket math, per-domain isolation, default fallback,
and concurrent-acquire serialization — all without live network calls.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from rate_limiter import DEFAULT_LIMITS, RateLimiter, _normalise_domain  # noqa: E402, I001


# ── Domain normalisation ────────────────────────────────────────────────────────


class TestNormaliseDomain:
    def test_strips_www(self):
        assert _normalise_domain("www.github.com") == "github.com"

    def test_strips_scheme(self):
        assert _normalise_domain("https://github.com/path") == "github.com"

    def test_strips_port(self):
        assert _normalise_domain("localhost:8080") == "localhost"

    def test_already_clean(self):
        assert _normalise_domain("reddit.com") == "reddit.com"

    def test_lowercased(self):
        assert _normalise_domain("GitHub.com") == "github.com"


# ── Token-bucket logic ─────────────────────────────────────────────────────────


class TestTokenBucket:
    async def test_single_acquire_succeeds(self):
        limiter = RateLimiter(defaults={"test.com": (5, 60)})
        # Should complete without blocking.
        await asyncio.wait_for(limiter.acquire("test.com"), timeout=1.0)

    async def test_acquire_n_times_within_period_succeeds(self):
        """Acquiring up to the limit should succeed immediately."""
        count, period = 5, 60
        limiter = RateLimiter(defaults={"test.com": (count, period)})
        for _ in range(count):
            await asyncio.wait_for(limiter.acquire("test.com"), timeout=1.0)

    async def test_exceed_limit_blocks_until_window_resets(self):
        """The (count+1)th acquire must wait for the window to roll."""
        count, period = 2, 0  # period=0 → window resets immediately on next check
        # Use a very short period so the test completes quickly.
        limiter = RateLimiter(defaults={"fast.com": (count, 1)})  # 2 per second

        # Consume the two tokens.
        await limiter.acquire("fast.com")
        await limiter.acquire("fast.com")

        # The third acquire must block briefly (< 2s) until the window rolls.
        await asyncio.wait_for(limiter.acquire("fast.com"), timeout=2.5)

    async def test_per_domain_isolation(self):
        """Exhausting one domain's bucket does not affect another domain."""
        limiter = RateLimiter(defaults={"a.com": (1, 60), "b.com": (5, 60)})
        await limiter.acquire("a.com")
        # a.com is now at limit. b.com should still be freely acquirable.
        await asyncio.wait_for(limiter.acquire("b.com"), timeout=1.0)

    async def test_default_fallback_for_unknown_domain(self):
        """Unknown domain falls back to the '*' entry in DEFAULT_LIMITS."""
        limiter = RateLimiter()
        # '*' defaults to (30, 60). One acquire should succeed immediately.
        await asyncio.wait_for(limiter.acquire("unknown-exotic-domain.io"), timeout=1.0)

    async def test_override_default_limits(self):
        """Caller-supplied overrides take precedence over DEFAULT_LIMITS."""
        limiter = RateLimiter(defaults={"github.com": (1, 60)})
        # First acquire is fine.
        await limiter.acquire("github.com")
        # Second acquire should block (limit is now 1, not the default 60).
        # We use a very short timeout to verify it DOES block.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire("github.com"), timeout=0.15)

    async def test_concurrent_acquires_serialise_correctly(self):
        """Multiple coroutines racing on the same domain should all succeed
        in order without data races."""
        count = 5
        limiter = RateLimiter(defaults={"concurrent.com": (count, 60)})
        results: list[str] = []

        async def worker(name: str) -> None:
            await limiter.acquire("concurrent.com")
            results.append(name)

        await asyncio.gather(*(worker(f"w{i}") for i in range(count)))
        assert len(results) == count
        assert sorted(results) == [f"w{i}" for i in range(count)]


class TestDefaultLimits:
    def test_github_present(self):
        assert "github.com" in DEFAULT_LIMITS

    def test_wildcard_present(self):
        assert "*" in DEFAULT_LIMITS

    def test_wildcard_is_30_per_60(self):
        assert DEFAULT_LIMITS["*"] == (30, 60)
