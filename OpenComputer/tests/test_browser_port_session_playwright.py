"""Unit tests for `session/playwright_session.py` and `session/target_id.py`.

Covers:
  - role-ref cache LRU behavior + 50-entry cap
  - role-ref cache survives a Page-object swap (lookups by target_id work
    after we replace the Playwright Page reference)
  - blocked target / page bookkeeping
  - get_page_for_target finds the right Page when CDP probe matches
  - get_page_for_target falls back to the single-page case when CDP fails
  - page_target_id queries CDP first, falls back to /json/list
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from extensions.browser_control.session import target_id as target_id_mod
from extensions.browser_control.session.playwright_session import (
    MAX_ROLE_REFS_CACHE,
    BlockedBrowserTargetError,
    PlaywrightSession,
    RoleRef,
)
from extensions.browser_control.session.target_id import page_target_id

# ─── fakes ────────────────────────────────────────────────────────────


class FakeCdpSession:
    def __init__(self, target_id: str | None) -> None:
        self._tid = target_id
        self.detached = False

    async def send(self, method: str) -> dict[str, Any]:
        if method != "Target.getTargetInfo":
            raise RuntimeError(f"unexpected method {method}")
        if self._tid is None:
            raise RuntimeError("Target.getTargetInfo: Not allowed")
        return {"targetInfo": {"targetId": self._tid}}

    async def detach(self) -> None:
        self.detached = True


class FakeContext:
    def __init__(self, *, cdp_target: str | None) -> None:
        self._tid = cdp_target

    async def new_cdp_session(self, _page: Any) -> FakeCdpSession:
        return FakeCdpSession(self._tid)


class FakePage:
    def __init__(self, *, url: str, cdp_target: str | None = None) -> None:
        self.url = url
        self.context = FakeContext(cdp_target=cdp_target)


class FakeBrowser:
    def __init__(self, contexts: list[Any]) -> None:
        self.contexts = contexts


class FakeBrowserContext:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages


# ─── role-ref cache ───────────────────────────────────────────────────


def _session() -> PlaywrightSession:
    return PlaywrightSession(browser=FakeBrowser([]), cdp_url="http://127.0.0.1:18800")


def test_role_refs_store_and_get() -> None:
    s = _session()
    refs = {"e1": RoleRef(role="button", name="OK")}
    s.store_role_refs(target_id="T1", refs=refs)
    entry = s.get_role_refs("T1")
    assert entry is not None
    assert entry.refs == refs


def test_role_refs_cache_lru_order_on_read() -> None:
    """LRU semantics — re-reading a key bumps it to the end."""
    s = _session()
    for i in range(5):
        s.store_role_refs(target_id=f"T{i}", refs={f"e{i}": RoleRef(role="button")})
    # Touch T0 — it should now be most-recently-used.
    assert s.get_role_refs("T0") is not None
    # Add a 6th entry; if we were FIFO, T0 would be next to evict, but LRU
    # makes T1 the oldest. (We don't observe eviction at size 6 since cap
    # is 50, but the order matters when we later cross the cap.)
    s.store_role_refs(target_id="T5", refs={"e5": RoleRef(role="button")})
    # Internal: keys() should have T0 nearer the end than T1.
    keys = list(s._role_refs.keys())
    assert keys.index("http://127.0.0.1:18800::T1") < keys.index(
        "http://127.0.0.1:18800::T0"
    )


def test_role_refs_cache_evicts_oldest_at_cap() -> None:
    s = _session()
    # Fill above the cap; oldest should be evicted.
    for i in range(MAX_ROLE_REFS_CACHE + 5):
        s.store_role_refs(target_id=f"T{i}", refs={f"e{i}": RoleRef(role="button")})
    assert s.role_refs_size() == MAX_ROLE_REFS_CACHE
    # T0..T4 should be evicted (oldest 5).
    for i in range(5):
        assert s.get_role_refs(f"T{i}") is None
    assert s.get_role_refs(f"T{MAX_ROLE_REFS_CACHE - 1}") is not None
    assert s.get_role_refs(f"T{MAX_ROLE_REFS_CACHE + 4}") is not None


def test_role_refs_survive_page_swap() -> None:
    """The cache is keyed by (cdp_url, target_id) — swapping the Page is a no-op for the cache."""
    s = _session()
    s.store_role_refs(target_id="T1", refs={"e1": RoleRef(role="button", name="Submit")})
    # Simulate the agent later asking for refs against a *different* Page
    # object that has the same target id (e.g. after a context reconnect).
    entry = s.get_role_refs("T1")
    assert entry is not None
    assert entry.refs["e1"].name == "Submit"


# ─── blocked tracking ─────────────────────────────────────────────────


def test_blocked_target_set() -> None:
    s = _session()
    assert not s.is_target_blocked("T1")
    s.mark_target_blocked("T1")
    assert s.is_target_blocked("T1")
    assert s.has_any_blocked_targets()
    s.clear_blocked_target("T1")
    assert not s.is_target_blocked("T1")


def test_blocked_page_weak_set() -> None:
    s = _session()

    class P:
        pass

    p = P()
    s.mark_page_blocked(p)
    assert s.is_page_blocked(p)
    s.clear_blocked_page(p)
    assert not s.is_page_blocked(p)


# ─── get_page_for_target ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_page_for_target_matches_via_cdp() -> None:
    p1 = FakePage(url="https://a.example/", cdp_target="T1")
    p2 = FakePage(url="https://b.example/", cdp_target="T2")
    s = PlaywrightSession(
        browser=FakeBrowser([FakeBrowserContext([p1, p2])]),
        cdp_url="http://127.0.0.1:18800",
    )
    found = await s.get_page_for_target("T2")
    assert found is p2


@pytest.mark.asyncio
async def test_get_page_for_target_blocked_raises() -> None:
    p1 = FakePage(url="https://a.example/", cdp_target="T1")
    s = PlaywrightSession(
        browser=FakeBrowser([FakeBrowserContext([p1])]),
        cdp_url="http://127.0.0.1:18800",
    )
    s.mark_target_blocked("T1")
    with pytest.raises(BlockedBrowserTargetError):
        await s.get_page_for_target("T1")


@pytest.mark.asyncio
async def test_get_page_for_target_single_page_fallback() -> None:
    """When CDP fails on the only Page, return it (extension context)."""
    page = FakePage(url="chrome-extension://abc/popup.html", cdp_target=None)
    s = PlaywrightSession(
        browser=FakeBrowser([FakeBrowserContext([page])]),
        cdp_url="http://127.0.0.1:18800",
    )
    found = await s.get_page_for_target("T1-extension")
    assert found is page


@pytest.mark.asyncio
async def test_get_page_for_target_no_match_raises() -> None:
    p1 = FakePage(url="https://a.example/", cdp_target="T1")
    p2 = FakePage(url="https://b.example/", cdp_target="T2")
    s = PlaywrightSession(
        browser=FakeBrowser([FakeBrowserContext([p1, p2])]),
        cdp_url="http://127.0.0.1:18800",
    )
    with pytest.raises(LookupError):
        await s.get_page_for_target("T-missing")


# ─── page_target_id (target_id.py) ────────────────────────────────────


@pytest.mark.asyncio
async def test_page_target_id_via_cdp() -> None:
    p = FakePage(url="https://example.com/", cdp_target="T1")
    assert await page_target_id(p, cdp_url="http://127.0.0.1:18800") == "T1"


@pytest.mark.asyncio
async def test_page_target_id_falls_back_to_json_list(monkeypatch) -> None:
    """When CDP fails, /json/list lookup returns the right id."""
    p = FakePage(url="https://example.com/", cdp_target=None)

    fake_targets = [
        {"id": "Tx", "url": "https://other.com/"},
        {"id": "Ty", "url": "https://example.com/"},
    ]

    class FakeResp:
        status_code = 200

        def json(self) -> Any:
            return fake_targets

    class FakeAsyncClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def get(self, url: str) -> Any:
            assert url.endswith("/json/list")
            return FakeResp()

    monkeypatch.setattr(target_id_mod.httpx, "AsyncClient", FakeAsyncClient)
    tid = await page_target_id(p, cdp_url="http://127.0.0.1:18800")
    assert tid == "Ty"


@pytest.mark.asyncio
async def test_page_target_id_handles_ws_cdp_url(monkeypatch) -> None:
    """A ws:// CDP URL with /devtools/browser/ prefix is rewritten for HTTP."""
    p = FakePage(url="https://example.com/", cdp_target=None)

    captured: dict[str, str] = {}

    class FakeResp:
        status_code = 200

        def json(self) -> Any:
            return [{"id": "Tz", "url": "https://example.com/"}]

    class FakeAsyncClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def get(self, url: str) -> Any:
            captured["url"] = url
            return FakeResp()

    monkeypatch.setattr(target_id_mod.httpx, "AsyncClient", FakeAsyncClient)
    tid = await page_target_id(
        p, cdp_url="ws://127.0.0.1:18800/devtools/browser/abc-123"
    )
    assert tid == "Tz"
    assert captured["url"] == "http://127.0.0.1:18800/json/list"


@pytest.mark.asyncio
async def test_page_target_id_returns_none_when_unknown(monkeypatch) -> None:
    p = FakePage(url="https://nowhere.example/", cdp_target=None)

    class FakeResp:
        status_code = 200

        def json(self) -> Any:
            return [{"id": "Tx", "url": "https://other.com/"}]

    class FakeAsyncClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def get(self, _url: str) -> Any:
            return FakeResp()

    monkeypatch.setattr(target_id_mod.httpx, "AsyncClient", FakeAsyncClient)
    tid = await page_target_id(p, cdp_url="http://127.0.0.1:18800")
    assert tid is None


@pytest.mark.asyncio
async def test_page_target_id_no_cdp_url_returns_none() -> None:
    p = FakePage(url="https://example.com/", cdp_target=None)
    assert await page_target_id(p, cdp_url=None) is None
