"""URL content-monitoring: poll a URL and detect hash-based changes.

``PageMonitor`` tracks the last-seen content hash per URL. ``monitor_loop``
is a convenience wrapper for snapshot + diff in a single call.

Real polling beyond ``max_iterations=1`` is the caller's responsibility
(schedule via cron / background task). Default ``max_iterations=1`` keeps
tests deterministic.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from wrapper import OpenCLIWrapper  # type: ignore[import-not-found]  # noqa: E402

log = logging.getLogger(__name__)


class PageMonitor:
    """Stateful URL monitor that compares content hashes across snapshots.

    Usage
    -----
    ::

        monitor = PageMonitor()
        snap = await monitor.snapshot(wrapper, "https://example.com/blog")
        # ... later ...
        diff = await monitor.diff(wrapper, "https://example.com/blog")
        if diff and diff["changed"]:
            print("Page has changed!")
    """

    def __init__(self) -> None:
        # url → {"content_hash": str, "fetched_at": float, "size_bytes": int}
        self._snapshots: dict[str, dict] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def snapshot(self, wrapper: OpenCLIWrapper, url: str) -> dict:
        """Fetch *url* and record its content hash.

        Parameters
        ----------
        wrapper:
            An ``OpenCLIWrapper`` instance. The method uses ``wrapper.run``
            with an empty adapter string (generic fetch) for URLs that don't
            match a known adapter.
        url:
            The URL to fetch.

        Returns
        -------
        dict
            ``{"url": str, "content_hash": str, "fetched_at": float,
               "size_bytes": int}``
        """
        raw = await _fetch_url(wrapper, url)
        content_bytes = _serialise(raw)
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        fetched_at = time.time()
        size_bytes = len(content_bytes)

        snap = {
            "url": url,
            "content_hash": content_hash,
            "fetched_at": fetched_at,
            "size_bytes": size_bytes,
        }
        self._snapshots[url] = snap
        return snap

    async def diff(self, wrapper: OpenCLIWrapper, url: str) -> dict | None:
        """Compare the current content of *url* to the last recorded snapshot.

        Returns ``None`` if no previous snapshot exists for *url*.

        Parameters
        ----------
        wrapper:
            An ``OpenCLIWrapper`` instance.
        url:
            The URL to check.

        Returns
        -------
        dict | None
            ``{"changed": bool, "old_hash": str, "new_hash": str,
               "delta_seconds": float}`` if a previous snapshot exists,
            ``None`` otherwise.
        """
        previous = self._snapshots.get(url)
        if previous is None:
            return None

        new_snap = await self.snapshot(wrapper, url)
        old_hash = previous["content_hash"]
        new_hash = new_snap["content_hash"]
        delta_seconds = new_snap["fetched_at"] - previous["fetched_at"]

        return {
            "changed": old_hash != new_hash,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "delta_seconds": delta_seconds,
        }

    def clear(self, url: str | None = None) -> None:
        """Reset snapshot state.

        Parameters
        ----------
        url:
            If provided, clear only this URL's snapshot. If ``None``,
            clear all snapshots.
        """
        if url is None:
            self._snapshots.clear()
        else:
            self._snapshots.pop(url, None)


async def monitor_loop(
    wrapper: OpenCLIWrapper,
    urls: list[str],
    interval_s: int,
    max_iterations: int = 1,
    on_change: Callable[[str, dict], None] | None = None,
) -> list[dict]:
    """Snapshot all URLs and return diffs that show changes.

    Parameters
    ----------
    wrapper:
        An ``OpenCLIWrapper`` instance.
    urls:
        List of URLs to monitor.
    interval_s:
        Polling interval between iterations in seconds. Ignored when
        ``max_iterations=1`` (the default — only one pass is done).
    max_iterations:
        Number of snapshot+diff cycles. Default ``1`` so tests don't hang.
        The *first* iteration always does a snapshot (no diff possible yet);
        subsequent iterations do the diff.
    on_change:
        Optional callback ``(url: str, diff: dict) -> None`` invoked whenever
        a URL's content changes between iterations.

    Returns
    -------
    list[dict]
        Diffs where ``changed == True``. Empty list if nothing changed or
        only one iteration ran (no baseline yet).
    """
    monitor = PageMonitor()
    changed_diffs: list[dict] = []

    for iteration in range(max_iterations):
        if iteration == 0:
            # First pass: establish baseline snapshots.
            for url in urls:
                try:
                    await monitor.snapshot(wrapper, url)
                except Exception as exc:
                    log.warning("monitor_loop: snapshot failed for %r — %s", url, exc)
        else:
            import asyncio

            await asyncio.sleep(interval_s)
            for url in urls:
                try:
                    result = await monitor.diff(wrapper, url)
                    if result and result["changed"]:
                        diff_entry = {"url": url, **result}
                        changed_diffs.append(diff_entry)
                        if on_change is not None:
                            on_change(url, diff_entry)
                except Exception as exc:
                    log.warning("monitor_loop: diff failed for %r — %s", url, exc)

    return changed_diffs


# ── Internal helpers ───────────────────────────────────────────────────────────


async def _fetch_url(wrapper: OpenCLIWrapper, url: str) -> object:
    """Invoke the wrapper with an empty adapter (generic fetch) for *url*.

    For known domains this degrades gracefully — the wrapper will try to
    spawn opencli with an empty adapter argument, which typically falls
    back to a basic GET. In tests the wrapper is mocked, so this always
    works regardless of actual opencli availability.
    """
    return await wrapper.run("", url)


def _serialise(obj: object) -> bytes:
    """Deterministically serialise *obj* to bytes for hashing."""
    import json

    return json.dumps(obj, sort_keys=True, default=str).encode("utf-8")


__all__ = ["PageMonitor", "monitor_loop"]
