"""CDP attach / disconnect — the connect deduper + force-disconnect path.

``connect_browser(cdp_url)``:

  1. Cache fast path — if we already have a live ConnectedBrowser, return it.
  2. In-flight dedup — if a concurrent caller is already connecting to the
     same URL, await its asyncio.Future. (Thundering-herd guard.)
  3. Retry loop, max 3 attempts, additive backoff 250 + n*250 ms.
     Per-attempt timeout: 5000 + n*2000 ms (5s, 7s, 9s).
  4. ``connect_over_cdp`` is wrapped in ``no_proxy_lease(endpoint)`` so
     loopback CDP is never routed through a system HTTP proxy.
  5. On disconnect, the cache is cleared via a guarded ``on_disconnected``
     listener that only deletes the entry if the dying browser is still
     the cached one (avoids racing with a fresh connect).

``force_disconnect_playwright_for_target(cdp_url, target_id)`` — the
"stuck JS" recovery path. Drops the cache entry, removes the disconnected
listener, optionally tries ``Runtime.terminateExecution`` over raw CDP,
and fires ``browser.close()`` fire-and-forget. **Never calls
``Connection.close()``** since Playwright shares one Connection across
all Browser objects (per BRIEF + deep dive).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from .helpers import no_proxy_lease, normalize_cdp_url, redact_cdp_url

_log = logging.getLogger("opencomputer.browser_control.session.cdp")


_MAX_ATTEMPTS: Final[int] = 3
_BASE_TIMEOUT_MS: Final[int] = 5_000
_TIMEOUT_INCREMENT_MS: Final[int] = 2_000
_BACKOFF_BASE_MS: Final[int] = 250
_BACKOFF_INCREMENT_MS: Final[int] = 250


# ─── data ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ConnectedBrowser:
    browser: Any
    cdp_url: str
    on_disconnected: Callable[[], None] | None = None


# Module-level state. The cache and in-flight maps are per-process — one
# AgentLoop is one process, and the dedup/cache only matter inside one.
_cached_by_cdp_url: dict[str, ConnectedBrowser] = {}
_connecting_by_cdp_url: dict[str, asyncio.Future[ConnectedBrowser]] = {}
_state_mutex = asyncio.Lock()

# The Playwright instance is also process-singleton — one runtime,
# any number of Browser objects underneath. Tests inject via the
# ``playwright`` kwarg on connect_browser.
_playwright_singleton: Any | None = None


async def _get_playwright(playwright: Any | None) -> Any:
    global _playwright_singleton
    if playwright is not None:
        return playwright
    if _playwright_singleton is not None:
        return _playwright_singleton
    from playwright.async_api import async_playwright  # imported lazily

    _playwright_singleton = await async_playwright().start()
    return _playwright_singleton


# ─── connect ─────────────────────────────────────────────────────────


async def connect_browser(
    cdp_url: str,
    *,
    playwright: Any | None = None,
    on_connect: Callable[[Any, str], None] | None = None,
    headers: dict[str, str] | None = None,
    max_attempts: int = _MAX_ATTEMPTS,
) -> ConnectedBrowser:
    """Connect to the CDP endpoint with dedup + retry + proxy bypass.

    The ``playwright`` kwarg is for tests — production callers leave it
    None so the lazy singleton is used. ``on_connect`` (also test-only)
    fires once per *fresh* connect (not on cache hits or dedup).
    """
    normalized = normalize_cdp_url(cdp_url)
    if not normalized:
        raise ValueError("connect_browser: cdp_url is empty")

    cached = _cached_by_cdp_url.get(normalized)
    if cached is not None:
        return cached

    # Acquire-or-create the in-flight future under the mutex so two
    # concurrent callers can't both observe "no future" and both create
    # their own.
    async with _state_mutex:
        cached = _cached_by_cdp_url.get(normalized)
        if cached is not None:
            return cached
        future = _connecting_by_cdp_url.get(normalized)
        if future is not None:
            pass  # someone else is already connecting; we'll await below
        else:
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            _connecting_by_cdp_url[normalized] = future
            connector_task = asyncio.create_task(
                _run_connect(
                    normalized,
                    future=future,
                    playwright=playwright,
                    headers=headers,
                    on_connect=on_connect,
                    max_attempts=max_attempts,
                )
            )
            future._connector_task = connector_task  # type: ignore[attr-defined]

    return await future


async def _run_connect(
    normalized: str,
    *,
    future: asyncio.Future[ConnectedBrowser],
    playwright: Any | None,
    headers: dict[str, str] | None,
    on_connect: Callable[[Any, str], None] | None,
    max_attempts: int,
) -> None:
    last_err: BaseException | None = None
    pw: Any | None = None
    try:
        pw = await _get_playwright(playwright)
        for attempt in range(max_attempts):
            timeout_ms = _BASE_TIMEOUT_MS + attempt * _TIMEOUT_INCREMENT_MS
            try:
                async with no_proxy_lease(normalized):
                    browser = await pw.chromium.connect_over_cdp(
                        normalized,
                        timeout=timeout_ms,
                        headers=headers or {},
                    )
            except Exception as exc:  # noqa: BLE001 — Playwright errors vary
                last_err = exc
                msg = str(exc).lower()
                if "rate limit" in msg:
                    # Don't retry rate-limit errors; they get worse.
                    break
                if attempt + 1 < max_attempts:
                    delay_ms = _BACKOFF_BASE_MS + attempt * _BACKOFF_INCREMENT_MS
                    await asyncio.sleep(delay_ms / 1000.0)
                continue

            connected = ConnectedBrowser(browser=browser, cdp_url=normalized)

            # Wire the disconnected listener so the cache is cleared if
            # the WS dies. The listener captures `connected`-by-identity
            # so a fresh connect doesn't get evicted by an old browser's
            # death.
            def _on_disconnected(_b: Any | None = None, _c: ConnectedBrowser = connected) -> None:
                cur = _cached_by_cdp_url.get(_c.cdp_url)
                if cur is _c:
                    _cached_by_cdp_url.pop(_c.cdp_url, None)
                _log.debug(
                    "connect_browser: %s disconnected; cache evicted",
                    redact_cdp_url(_c.cdp_url),
                )

            connected.on_disconnected = _on_disconnected
            on = getattr(browser, "on", None)
            if callable(on):
                try:
                    on("disconnected", _on_disconnected)
                except Exception:  # noqa: BLE001
                    pass

            _cached_by_cdp_url[normalized] = connected
            if on_connect is not None:
                try:
                    on_connect(browser, normalized)
                except Exception as exc:  # noqa: BLE001
                    _log.debug("connect_browser: on_connect callback raised: %s", exc)

            future.set_result(connected)
            return

        if last_err is None:
            last_err = RuntimeError(
                f"connect_browser: failed after {max_attempts} attempts "
                f"(no underlying error)"
            )
        future.set_exception(last_err)
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
        raise
    finally:
        async with _state_mutex:
            _connecting_by_cdp_url.pop(normalized, None)


# ─── force disconnect ─────────────────────────────────────────────────


async def force_disconnect_playwright_for_target(
    cdp_url: str,
    target_id: str | None = None,
    *,
    raw_cdp_send: Callable[..., Any] | None = None,
) -> None:
    """The stuck-JS recovery path.

    Steps mirror pw-session.ts:1023-1057:

      1. Drop the cache entry + the in-flight promise (so the next
         ``connect_browser`` makes a fresh connection).
      2. Remove the ``disconnected`` listener (so the dying browser
         doesn't race with the next connect).
      3. Best-effort ``Runtime.terminateExecution`` via raw CDP if
         ``target_id`` is known and ``raw_cdp_send`` is provided
         (tests stub this).
      4. Fire-and-forget ``browser.close()`` — never awaited; never
         touch ``Connection.close()`` (would corrupt other Browsers).
    """
    normalized = normalize_cdp_url(cdp_url)
    cur = _cached_by_cdp_url.pop(normalized, None)
    _connecting_by_cdp_url.pop(normalized, None)

    if cur is None:
        return

    off = getattr(cur.browser, "remove_listener", None) or getattr(cur.browser, "off", None)
    if cur.on_disconnected is not None and callable(off):
        try:
            off("disconnected", cur.on_disconnected)
        except Exception:  # noqa: BLE001
            pass

    if target_id and raw_cdp_send is not None:
        try:
            await raw_cdp_send("Runtime.terminateExecution", {"targetId": target_id})
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "force_disconnect_playwright_for_target: terminateExecution failed: %s",
                exc,
            )

    close = getattr(cur.browser, "close", None)
    if callable(close):
        # Fire-and-forget — never await. If close() hangs forever, the
        # next connect_browser still works because we already evicted
        # the cache entry.
        async def _swallow() -> None:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001
                _log.debug(
                    "force_disconnect_playwright_for_target: close raised: %s", exc
                )

        try:
            asyncio.create_task(_swallow())
        except RuntimeError:
            # No running loop — best-effort, ignore.
            pass


# ─── test-only helpers ───────────────────────────────────────────────


def _reset_state_for_tests() -> None:
    """Drop all cached + in-flight state. Tests call this in fixtures."""
    _cached_by_cdp_url.clear()
    for fut in list(_connecting_by_cdp_url.values()):
        if not fut.done():
            fut.cancel()
    _connecting_by_cdp_url.clear()


def _peek_cached() -> dict[str, ConnectedBrowser]:
    return dict(_cached_by_cdp_url)


def _peek_inflight() -> dict[str, asyncio.Future[ConnectedBrowser]]:
    return dict(_connecting_by_cdp_url)
