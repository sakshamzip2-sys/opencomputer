"""Unit tests for browser-port `session/cdp.py` (Wave 1a).

Covers:
  - connect_browser dedupes concurrent calls to the same URL
  - retries with additive backoff (mocked clock)
  - rate-limit errors short-circuit retry
  - on-disconnect listener evicts the cache (and only for the matching browser)
  - force_disconnect_playwright_for_target drops the cache + fires close
    fire-and-forget without awaiting it
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from extensions.browser_control.session import cdp as cdp_mod
from extensions.browser_control.session.cdp import (
    ConnectedBrowser,
    connect_browser,
    force_disconnect_playwright_for_target,
)

# ─── fakes ────────────────────────────────────────────────────────────


class FakeBrowser:
    def __init__(self, label: str = "br") -> None:
        self.label = label
        self.listeners: dict[str, list[Any]] = {}
        self.close_called = 0

    def on(self, event: str, cb: Any) -> None:
        self.listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event: str, cb: Any) -> None:
        if event in self.listeners and cb in self.listeners[event]:
            self.listeners[event].remove(cb)

    async def close(self) -> None:
        self.close_called += 1


class FakeChromium:
    def __init__(self, *, on_connect: Any) -> None:
        self._on_connect = on_connect

    async def connect_over_cdp(self, *args: Any, **kwargs: Any) -> Any:
        return await self._on_connect(*args, **kwargs)


class FakePlaywright:
    def __init__(self, *, on_connect: Any) -> None:
        self.chromium = FakeChromium(on_connect=on_connect)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    cdp_mod._reset_state_for_tests()


# ─── connect_browser ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_browser_returns_browser_and_caches() -> None:
    calls = 0
    fb = FakeBrowser()

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        nonlocal calls
        calls += 1
        return fb

    pw = FakePlaywright(on_connect=on_connect)

    result = await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert isinstance(result, ConnectedBrowser)
    assert result.browser is fb
    assert calls == 1
    # Second call hits cache — connect_over_cdp not re-invoked.
    again = await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert again is result
    assert calls == 1


@pytest.mark.asyncio
async def test_connect_browser_normalizes_trailing_slash() -> None:
    fb = FakeBrowser()

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        return fb

    pw = FakePlaywright(on_connect=on_connect)
    a = await connect_browser("http://127.0.0.1:18800/", playwright=pw)
    b = await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert a is b


@pytest.mark.asyncio
async def test_connect_browser_dedupes_concurrent_calls() -> None:
    """5 concurrent calls to the same URL → connect_over_cdp runs once."""
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    fb = FakeBrowser()

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return fb

    pw = FakePlaywright(on_connect=on_connect)
    tasks = [
        asyncio.create_task(connect_browser("http://127.0.0.1:18800", playwright=pw))
        for _ in range(5)
    ]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1
    assert all(r is results[0] for r in results)


@pytest.mark.asyncio
async def test_connect_browser_retries_on_failure_with_backoff() -> None:
    attempts = 0
    fb = FakeBrowser()
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionRefusedError("no chrome yet")
        return fb

    pw = FakePlaywright(on_connect=on_connect)
    real_sleep = asyncio.sleep
    try:
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        result = await connect_browser("http://127.0.0.1:18800", playwright=pw)
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    assert result.browser is fb
    assert attempts == 3
    # Backoff after attempt 0 = 250ms, after attempt 1 = 500ms.
    # Backoff loop runs only between failed attempts (so sleeps before
    # the 3rd attempt = 250 then 500). Tolerate any small additional
    # internal sleeps from the proxy lease (none expected for non-loopback,
    # but the lease still acquires — assert at least the two expected delays).
    assert pytest.approx(sleeps[0], rel=0.0) == 0.25
    assert pytest.approx(sleeps[1], rel=0.0) == 0.5


@pytest.mark.asyncio
async def test_connect_browser_rate_limit_breaks_retry() -> None:
    attempts = 0

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("rate limit exceeded")

    pw = FakePlaywright(on_connect=on_connect)
    with pytest.raises(RuntimeError, match="rate limit"):
        await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert attempts == 1


@pytest.mark.asyncio
async def test_connect_browser_failure_clears_inflight() -> None:
    """A failed connect must clear the in-flight future so the next call retries."""
    n = 0

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        nonlocal n
        n += 1
        raise RuntimeError("rate limit hit")

    pw = FakePlaywright(on_connect=on_connect)
    with pytest.raises(RuntimeError):
        await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert cdp_mod._peek_inflight() == {}
    # And a second connect actually retries (not blocked by the dead future).
    with pytest.raises(RuntimeError):
        await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert n == 2


@pytest.mark.asyncio
async def test_disconnected_listener_evicts_cache() -> None:
    fb = FakeBrowser()

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        return fb

    pw = FakePlaywright(on_connect=on_connect)
    connected = await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert "http://127.0.0.1:18800" in cdp_mod._peek_cached()

    # Fire the disconnected listener.
    listeners = fb.listeners.get("disconnected", [])
    assert len(listeners) == 1
    listeners[0]()
    assert "http://127.0.0.1:18800" not in cdp_mod._peek_cached()


@pytest.mark.asyncio
async def test_disconnected_listener_no_op_for_stale_browser() -> None:
    """A stale disconnect must not evict a fresh cached entry."""
    old = FakeBrowser("old")
    new = FakeBrowser("new")
    seq = [old, new]

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        return seq.pop(0)

    pw = FakePlaywright(on_connect=on_connect)
    await connect_browser("http://127.0.0.1:18800", playwright=pw)
    # Capture the listener closure registered for the OLD browser before
    # force_disconnect removes it.
    old_listeners = list(old.listeners.get("disconnected", []))
    assert len(old_listeners) == 1
    old_disconnect_cb = old_listeners[0]

    # Drop the cache so a fresh connect can re-occupy it.
    await force_disconnect_playwright_for_target("http://127.0.0.1:18800")
    assert "http://127.0.0.1:18800" not in cdp_mod._peek_cached()

    fresh = await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert fresh.browser is new

    # Fire the OLD browser's disconnected listener — must NOT evict
    # the fresh cache entry, since the closure compares by identity.
    old_disconnect_cb()
    cur = cdp_mod._peek_cached().get("http://127.0.0.1:18800")
    assert cur is not None and cur.browser is new


# ─── force_disconnect ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_disconnect_drops_cache_and_fires_close() -> None:
    fb = FakeBrowser()

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        return fb

    pw = FakePlaywright(on_connect=on_connect)
    await connect_browser("http://127.0.0.1:18800", playwright=pw)
    assert "http://127.0.0.1:18800" in cdp_mod._peek_cached()

    await force_disconnect_playwright_for_target("http://127.0.0.1:18800")
    # Cache cleared synchronously.
    assert "http://127.0.0.1:18800" not in cdp_mod._peek_cached()
    # The disconnected listener for fb was removed.
    assert fb.listeners.get("disconnected", []) == []
    # close() is fire-and-forget — give it a chance to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert fb.close_called == 1


@pytest.mark.asyncio
async def test_force_disconnect_calls_terminate_when_provided() -> None:
    fb = FakeBrowser()

    async def on_connect(*_a: Any, **_k: Any) -> Any:
        return fb

    pw = FakePlaywright(on_connect=on_connect)
    await connect_browser("http://127.0.0.1:18800", playwright=pw)

    sent: list[tuple[str, dict[str, Any]]] = []

    async def raw_send(method: str, params: dict[str, Any]) -> None:
        sent.append((method, params))

    await force_disconnect_playwright_for_target(
        "http://127.0.0.1:18800",
        target_id="T1",
        raw_cdp_send=raw_send,
    )
    assert sent == [("Runtime.terminateExecution", {"targetId": "T1"})]


@pytest.mark.asyncio
async def test_force_disconnect_no_cache_is_safe() -> None:
    # Should not raise even if there's nothing to disconnect.
    await force_disconnect_playwright_for_target("http://127.0.0.1:18800")
