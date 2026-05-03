"""Cookies + localStorage + sessionStorage tooling.

Security model (deep dive §8):

  - Cookies: ``context.cookies()`` / ``context.add_cookies()`` —
    **context-wide**, all origins in the browser context.
  - localStorage / sessionStorage: ``page.evaluate`` against
    ``window.localStorage`` / ``window.sessionStorage`` —
    **page (origin) -scoped**.

The Python port preserves this. Don't accidentally hoist storage to
context level.
"""

from __future__ import annotations

import json
from typing import Any, Literal

StorageKind = Literal["local", "session"]

# ─── cookies ─────────────────────────────────────────────────────────


async def get_cookies(context: Any) -> list[dict[str, Any]]:
    """Read all cookies in the context."""
    cookies = await context.cookies()
    return list(cookies or [])


async def add_cookie(context: Any, cookie: dict[str, Any]) -> None:
    """Add a single cookie. Validates url XOR domain+path."""
    if not isinstance(cookie, dict):
        raise TypeError("cookie must be a dict")
    if not cookie.get("name"):
        raise ValueError("cookie.name is required")
    if "value" not in cookie:
        raise ValueError("cookie.value is required")
    has_url = bool(cookie.get("url"))
    has_domain_path = bool(cookie.get("domain")) and bool(cookie.get("path"))
    if not has_url and not has_domain_path:
        raise ValueError("cookie requires url, or domain+path")
    await context.add_cookies([cookie])


async def clear_cookies(context: Any) -> None:
    """Wipe every cookie in the context (all origins)."""
    await context.clear_cookies()


# ─── localStorage / sessionStorage ───────────────────────────────────


def _store_name(kind: StorageKind) -> str:
    if kind == "local":
        return "localStorage"
    if kind == "session":
        return "sessionStorage"
    raise ValueError(f"unknown storage kind: {kind!r}")


async def storage_get(
    page: Any, kind: StorageKind, *, key: str | None = None
) -> dict[str, str]:
    """Read ``localStorage`` or ``sessionStorage``.

    ``key=None`` → dump all entries. ``key`` set → return ``{key: value}``
    or ``{}`` when the key is missing.
    """
    store = _store_name(kind)
    if key is not None:
        if not key:
            raise ValueError("key must be non-empty when provided")
        js = f"() => {{ const v = window.{store}.getItem({json.dumps(key)}); return v === null ? {{}} : {{ {json.dumps(key)}: v }}; }}"
        result = await page.evaluate(js)
        return dict(result or {})
    js_all = (
        f"() => {{ const o = {{}}; for (let i = 0; i < window.{store}.length; i++) "
        f"{{ const k = window.{store}.key(i); o[k] = window.{store}.getItem(k); }} return o; }}"
    )
    result = await page.evaluate(js_all)
    return dict(result or {})


async def storage_set(page: Any, kind: StorageKind, *, key: str, value: str) -> None:
    """Set one entry."""
    if not key:
        raise ValueError("key is required")
    store = _store_name(kind)
    js = f"(args) => {{ window.{store}.setItem(args.k, args.v); }}"
    await page.evaluate(js, {"k": key, "v": value})


async def storage_remove(page: Any, kind: StorageKind, *, key: str) -> None:
    if not key:
        raise ValueError("key is required")
    store = _store_name(kind)
    js = f"(k) => {{ window.{store}.removeItem(k); }}"
    await page.evaluate(js, key)


async def storage_clear(page: Any, kind: StorageKind) -> None:
    """Clear ``localStorage`` or ``sessionStorage`` for the active origin."""
    store = _store_name(kind)
    js = f"() => {{ window.{store}.clear(); }}"
    await page.evaluate(js)
