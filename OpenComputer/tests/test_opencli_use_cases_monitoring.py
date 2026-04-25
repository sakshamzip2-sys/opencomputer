"""Tests for extensions/opencli-scraper/use_cases/content_monitoring.py.

Uses mocked wrappers — no live opencli binary needed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

_PLUGIN_DIR = Path(__file__).parent.parent / "extensions" / "opencli-scraper"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from use_cases.content_monitoring import PageMonitor, monitor_loop  # noqa: E402

# ── Helpers ────────────────────────────────────────────────────────────────────

_URL = "https://example.com/blog"
_CONTENT_A = {"title": "Hello", "body": "World"}
_CONTENT_B = {"title": "Hello", "body": "CHANGED"}


def _make_wrapper(*responses):
    """Wrapper whose run() returns responses in sequence."""
    wrapper = MagicMock()
    wrapper.run = AsyncMock(side_effect=list(responses))
    return wrapper


def _hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# ── Tests: PageMonitor.snapshot ───────────────────────────────────────────────


class TestPageMonitorSnapshot:
    async def test_snapshot_returns_expected_keys(self):
        wrapper = _make_wrapper(_CONTENT_A)
        monitor = PageMonitor()
        snap = await monitor.snapshot(wrapper, _URL)

        assert snap["url"] == _URL
        assert "content_hash" in snap
        assert "fetched_at" in snap
        assert "size_bytes" in snap

    async def test_snapshot_content_hash_matches_sha256(self):
        wrapper = _make_wrapper(_CONTENT_A)
        monitor = PageMonitor()
        snap = await monitor.snapshot(wrapper, _URL)

        expected_hash = _hash(_CONTENT_A)
        assert snap["content_hash"] == expected_hash

    async def test_snapshot_size_bytes_positive(self):
        wrapper = _make_wrapper(_CONTENT_A)
        monitor = PageMonitor()
        snap = await monitor.snapshot(wrapper, _URL)
        assert snap["size_bytes"] > 0

    async def test_snapshot_stores_state(self):
        """After snapshot, monitor internal state should have the URL."""
        wrapper = _make_wrapper(_CONTENT_A)
        monitor = PageMonitor()
        await monitor.snapshot(wrapper, _URL)
        assert _URL in monitor._snapshots


# ── Tests: PageMonitor.diff ───────────────────────────────────────────────────


class TestPageMonitorDiff:
    async def test_diff_returns_none_when_no_previous_snapshot(self):
        wrapper = _make_wrapper(_CONTENT_A)
        monitor = PageMonitor()
        result = await monitor.diff(wrapper, _URL)
        assert result is None

    async def test_diff_returns_changed_true_when_content_differs(self):
        wrapper = _make_wrapper(_CONTENT_A, _CONTENT_B)
        monitor = PageMonitor()
        await monitor.snapshot(wrapper, _URL)
        result = await monitor.diff(wrapper, _URL)

        assert result is not None
        assert result["changed"] is True
        assert result["old_hash"] != result["new_hash"]

    async def test_diff_returns_changed_false_when_content_same(self):
        wrapper = _make_wrapper(_CONTENT_A, _CONTENT_A)
        monitor = PageMonitor()
        await monitor.snapshot(wrapper, _URL)
        result = await monitor.diff(wrapper, _URL)

        assert result is not None
        assert result["changed"] is False
        assert result["old_hash"] == result["new_hash"]

    async def test_diff_includes_delta_seconds(self):
        wrapper = _make_wrapper(_CONTENT_A, _CONTENT_B)
        monitor = PageMonitor()
        await monitor.snapshot(wrapper, _URL)
        result = await monitor.diff(wrapper, _URL)

        assert result is not None
        assert "delta_seconds" in result
        assert result["delta_seconds"] >= 0.0

    async def test_diff_returns_both_hashes(self):
        wrapper = _make_wrapper(_CONTENT_A, _CONTENT_B)
        monitor = PageMonitor()
        await monitor.snapshot(wrapper, _URL)
        result = await monitor.diff(wrapper, _URL)

        assert result is not None
        assert "old_hash" in result
        assert "new_hash" in result


# ── Tests: PageMonitor.clear ──────────────────────────────────────────────────


class TestPageMonitorClear:
    async def test_clear_all_resets_state(self):
        wrapper = _make_wrapper(_CONTENT_A, _CONTENT_A)
        monitor = PageMonitor()
        await monitor.snapshot(wrapper, _URL)
        assert _URL in monitor._snapshots

        monitor.clear()
        assert _URL not in monitor._snapshots

    async def test_clear_specific_url_removes_only_that_url(self):
        other_url = "https://other.example.com"
        wrapper = _make_wrapper(_CONTENT_A, _CONTENT_A)
        monitor = PageMonitor()
        await monitor.snapshot(wrapper, _URL)

        # Inject a second URL into state directly.
        monitor._snapshots[other_url] = {"content_hash": "abc", "fetched_at": 0.0, "size_bytes": 1, "url": other_url}

        monitor.clear(_URL)
        assert _URL not in monitor._snapshots
        assert other_url in monitor._snapshots

    async def test_clear_after_clear_is_idempotent(self):
        monitor = PageMonitor()
        monitor.clear()  # should not raise
        monitor.clear()  # still should not raise


# ── Tests: monitor_loop ───────────────────────────────────────────────────────


class TestMonitorLoop:
    async def test_monitor_loop_max_iterations_1_returns_empty(self):
        """With max_iterations=1, only snapshots are taken — no diffs yet."""
        wrapper = _make_wrapper(_CONTENT_A)
        result = await monitor_loop(wrapper, [_URL], interval_s=0, max_iterations=1)
        assert result == []

    async def test_monitor_loop_on_change_callback_fires_on_change(self):
        """Callback fires when a URL changes between iteration 0 and 1."""
        wrapper = _make_wrapper(_CONTENT_A, _CONTENT_B)

        fired: list[dict] = []

        def on_change(url: str, diff: dict) -> None:
            fired.append({"url": url, "diff": diff})

        result = await monitor_loop(
            wrapper, [_URL], interval_s=0, max_iterations=2, on_change=on_change
        )

        assert len(fired) == 1
        assert fired[0]["url"] == _URL
        assert len(result) == 1
        assert result[0]["changed"] is True
