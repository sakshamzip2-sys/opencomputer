"""Unit tests for ``tools_core.storage`` cookies + local/session storage."""

from __future__ import annotations

import json
from typing import Any

import pytest
from extensions.browser_control.tools_core.storage import (
    add_cookie,
    clear_cookies,
    get_cookies,
    storage_clear,
    storage_get,
    storage_remove,
    storage_set,
)


class _MockContext:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.cleared = 0
        self.cookies_jar: list[dict[str, Any]] = []

    async def cookies(self) -> list[dict[str, Any]]:
        return list(self.cookies_jar)

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.added.extend(cookies)
        self.cookies_jar.extend(cookies)

    async def clear_cookies(self) -> None:
        self.cleared += 1
        self.cookies_jar.clear()


class _MockPage:
    """Tiny localStorage simulator: ``page.evaluate`` runs a string of JS,
    we just inspect the body for known patterns and update an internal dict.
    """

    def __init__(self) -> None:
        self.local_storage: dict[str, dict[str, str]] = {"local": {}, "session": {}}
        self.eval_log: list[tuple[str, Any]] = []

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        self.eval_log.append((js, arg))
        if "localStorage" in js:
            store = "local"
        elif "sessionStorage" in js:
            store = "session"
        else:
            return None

        bag = self.local_storage[store]

        if "getItem" in js:
            # single key get; key is JSON-encoded into the JS string
            key = arg if isinstance(arg, str) else _extract_first_json_string(js)
            v = bag.get(key)
            return {} if v is None else {key: v}
        if ".setItem(" in js:
            assert isinstance(arg, dict)
            bag[arg["k"]] = arg["v"]
            return None
        if ".removeItem(" in js:
            assert isinstance(arg, str)
            bag.pop(arg, None)
            return None
        if ".clear(" in js:
            bag.clear()
            return None
        if "for (let i = 0;" in js:
            return dict(bag)
        return None


def _extract_first_json_string(js: str) -> str:
    """Best-effort: find the first JSON-encoded string literal in the JS."""
    import re

    m = re.search(r'"([^"]+)"', js)
    return m.group(1) if m else ""


# ─── cookies ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_cookie_validates_url_or_domain_path() -> None:
    ctx = _MockContext()
    with pytest.raises(ValueError, match="cookie requires url"):
        await add_cookie(ctx, {"name": "a", "value": "b"})


@pytest.mark.asyncio
async def test_add_cookie_with_url_ok() -> None:
    ctx = _MockContext()
    await add_cookie(ctx, {"name": "a", "value": "b", "url": "https://x.com"})
    assert ctx.added[0]["name"] == "a"


@pytest.mark.asyncio
async def test_add_cookie_with_domain_path_ok() -> None:
    ctx = _MockContext()
    await add_cookie(ctx, {"name": "a", "value": "b", "domain": "x.com", "path": "/"})
    assert ctx.added[0]["domain"] == "x.com"


@pytest.mark.asyncio
async def test_add_cookie_requires_value_key() -> None:
    ctx = _MockContext()
    with pytest.raises(ValueError, match="cookie.value is required"):
        await add_cookie(ctx, {"name": "a", "url": "https://x.com"})


@pytest.mark.asyncio
async def test_get_cookies_round_trip() -> None:
    ctx = _MockContext()
    await add_cookie(ctx, {"name": "a", "value": "1", "url": "https://x.com"})
    await add_cookie(ctx, {"name": "b", "value": "2", "url": "https://y.com"})
    cookies = await get_cookies(ctx)
    assert len(cookies) == 2


@pytest.mark.asyncio
async def test_clear_cookies_wipes() -> None:
    ctx = _MockContext()
    await add_cookie(ctx, {"name": "a", "value": "1", "url": "https://x.com"})
    await clear_cookies(ctx)
    assert ctx.cleared == 1
    assert ctx.cookies_jar == []


# ─── localStorage / sessionStorage ───────────────────────────────────


@pytest.mark.asyncio
async def test_storage_set_then_get_local() -> None:
    page = _MockPage()
    await storage_set(page, "local", key="k", value="v")
    out = await storage_get(page, "local", key="k")
    assert out == {"k": "v"}


@pytest.mark.asyncio
async def test_storage_set_then_get_session() -> None:
    page = _MockPage()
    await storage_set(page, "session", key="x", value="y")
    out = await storage_get(page, "session", key="x")
    assert out == {"x": "y"}


@pytest.mark.asyncio
async def test_storage_remove() -> None:
    page = _MockPage()
    await storage_set(page, "local", key="k", value="v")
    await storage_remove(page, "local", key="k")
    out = await storage_get(page, "local", key="k")
    assert out == {}


@pytest.mark.asyncio
async def test_storage_clear_kind() -> None:
    page = _MockPage()
    await storage_set(page, "local", key="k", value="v")
    await storage_clear(page, "local")
    out = await storage_get(page, "local")
    assert out == {}


@pytest.mark.asyncio
async def test_storage_set_blank_key_raises() -> None:
    page = _MockPage()
    with pytest.raises(ValueError):
        await storage_set(page, "local", key="", value="v")


@pytest.mark.asyncio
async def test_storage_unknown_kind_raises() -> None:
    page = _MockPage()
    with pytest.raises(ValueError):
        await storage_get(page, "global", key=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_local_and_session_are_independent() -> None:
    """Reading session after setting only local must return empty —
    ensures we don't conflate the two stores in our mock or in the
    helper module."""
    page = _MockPage()
    await storage_set(page, "local", key="k", value="local-v")
    out = await storage_get(page, "session")
    assert "k" not in out


# ensure mock self-test
def test_mock_extracts_json_string() -> None:
    js = 'window.localStorage.getItem("hello")'
    assert _extract_first_json_string(js) == "hello"
    # confirms the helper actually decodes.
    json.loads('"hello"')
