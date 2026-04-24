"""Tests for extensions/opencli-scraper/robots_cache.py.

All HTTP calls are mocked — no live network access.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from robots_cache import USER_AGENT, RobotsCache  # noqa: E402, I001


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_http_response(status: int, text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


def _make_httpx_client(status: int, text: str) -> MagicMock:
    """Return an async context manager that yields a mock httpx.AsyncClient."""
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_http_response(status, text))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


_DISALLOW_ALL = """\
User-agent: *
Disallow: /
"""

_ALLOW_ALL = """\
User-agent: *
Allow: /
"""

_DISALLOW_OTHER_UA = """\
User-agent: Googlebot
Disallow: /
User-agent: *
Allow: /
"""

_DISALLOW_PATH = """\
User-agent: *
Disallow: /private/
"""


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestRobotsCacheTTL:
    async def test_cached_entry_returns_same_result(self):
        """Second call to allowed() with fresh cache must NOT re-fetch."""
        cm = _make_httpx_client(200, _ALLOW_ALL)
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            r1 = await cache.allowed("https://example.com/page")
            r2 = await cache.allowed("https://example.com/page")

        assert r1 is True
        assert r2 is True
        # Only one HTTP fetch despite two allowed() calls.
        assert cm.__aenter__.return_value.get.call_count == 1

    async def test_expired_entry_refetches(self):
        """After TTL expires the cache must re-fetch robots.txt."""
        call_count = 0

        async def fake_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_http_response(200, _ALLOW_ALL)

        cm = MagicMock()
        client = MagicMock()
        client.get = fake_get
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)

        # Simulate time passing: first call at t=0, then populate cache with
        # fetched_at = t=0 but then artificially set it to an expired time.
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            await cache.allowed("https://stale.com/page")  # fetches (call_count=1)
            # Manually expire the cache entry.
            cache._cache["stale.com"].fetched_at = time.monotonic() - 90000
            await cache.allowed("https://stale.com/page")  # re-fetches (call_count=2)

        assert call_count == 2

    async def test_allow_on_404(self):
        cm = _make_httpx_client(404, "")
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            result = await cache.allowed("https://norules.com/anything")
        assert result is True

    async def test_deny_on_5xx(self):
        cm = _make_httpx_client(503, "Service Unavailable")
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            result = await cache.allowed("https://blocking.com/page")
        assert result is False

    async def test_disallow_all_denies(self):
        cm = _make_httpx_client(200, _DISALLOW_ALL)
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            result = await cache.allowed("https://denied.com/anything")
        assert result is False

    async def test_allow_all_allows(self):
        cm = _make_httpx_client(200, _ALLOW_ALL)
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            result = await cache.allowed("https://open.com/page")
        assert result is True

    async def test_disallow_for_other_ua_still_allows_ours(self):
        """A Disallow targeting Googlebot should not affect our User-Agent."""
        cm = _make_httpx_client(200, _DISALLOW_OTHER_UA)
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            result = await cache.allowed("https://mixed.com/page")
        # _DISALLOW_OTHER_UA disallows Googlebot but allows * → should allow
        assert result is True

    async def test_disallow_specific_path_allows_other_paths(self):
        cm = _make_httpx_client(200, _DISALLOW_PATH)
        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            denied = await cache.allowed("https://example.com/private/secret")
            allowed = await cache.allowed("https://example.com/public/page")
        assert denied is False
        assert allowed is True

    async def test_network_error_denies(self):
        """Network errors (non-404/5xx) should result in deny."""
        cm = MagicMock()
        client = MagicMock()
        client.get = AsyncMock(side_effect=Exception("connection refused"))
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=cm):
            cache = RobotsCache()
            result = await cache.allowed("https://unreachable.com/page")
        assert result is False
