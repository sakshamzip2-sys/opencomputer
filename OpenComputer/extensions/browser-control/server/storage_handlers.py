"""Storage + emulation handlers — cookies, local/session storage,
offline/headers/credentials/geolocation/locale/timezone/device.

Cookies hit the context; storage hits the page (origin-scoped).
Emulation knobs run via tools_core.state.
"""

from __future__ import annotations

from typing import Any

from ..tools_core import (
    add_cookie,
    clear_cookies,
    emulate_color_scheme,
    emulate_device,
    get_cookies,
    set_extra_http_headers,
    set_geolocation,
    set_http_credentials,
    set_locale,
    set_offline,
    set_timezone,
    storage_clear,
    storage_get,
    storage_remove,
    storage_set,
)
from .agent_handlers import _resolve_page
from .handlers import BrowserHandlerError, BrowserRouteContext


def _validate_kind(kind: str) -> str:
    if kind not in ("local", "session"):
        raise BrowserHandlerError(
            f"unknown storage kind: {kind!r}", status=400, code="bad_kind"
        )
    return kind


async def handle_get_cookies(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    cookies = await get_cookies(page.context)
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "cookies": cookies}


async def handle_set_cookie(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    cookie: dict[str, Any],
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        await add_cookie(page.context, cookie)
    except (TypeError, ValueError) as exc:
        raise BrowserHandlerError(
            str(exc), status=400, code="bad_cookie"
        ) from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_clear_cookies(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    await clear_cookies(page.context)
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_storage_get(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    kind: str,
    key: str | None = None,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    k = _validate_kind(kind)
    try:
        result = await storage_get(page, k, key=key)  # type: ignore[arg-type]
    except ValueError as exc:
        raise BrowserHandlerError(
            str(exc), status=400, code="bad_key"
        ) from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "kind": k, "items": result}


async def handle_storage_set(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    kind: str,
    key: str,
    value: str,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    k = _validate_kind(kind)
    try:
        await storage_set(page, k, key=key, value=value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise BrowserHandlerError(
            str(exc), status=400, code="bad_key"
        ) from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "kind": k}


async def handle_storage_clear(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    kind: str,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    k = _validate_kind(kind)
    await storage_clear(page, k)  # type: ignore[arg-type]
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "kind": k}


async def handle_storage_remove(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    kind: str,
    key: str,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    k = _validate_kind(kind)
    try:
        await storage_remove(page, k, key=key)  # type: ignore[arg-type]
    except ValueError as exc:
        raise BrowserHandlerError(
            str(exc), status=400, code="bad_key"
        ) from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "kind": k}


# ─── emulation ───────────────────────────────────────────────────────


async def handle_set_offline(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None, offline: bool
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    await set_offline(page.context, bool(offline))
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_set_headers(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    headers: dict[str, str],
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        await set_extra_http_headers(page.context, headers)
    except TypeError as exc:
        raise BrowserHandlerError(str(exc), status=400, code="bad_headers") from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_set_credentials(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    username: str | None = None,
    password: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    await set_http_credentials(
        page.context, username=username, password=password, clear=bool(clear)
    )
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_set_geolocation(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    latitude: float | None = None,
    longitude: float | None = None,
    accuracy: float | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    await set_geolocation(
        page.context,
        latitude=latitude,
        longitude=longitude,
        accuracy=accuracy,
        clear=bool(clear),
    )
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_set_media(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    color_scheme: str,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        await emulate_color_scheme(page, color_scheme)
    except ValueError as exc:
        raise BrowserHandlerError(str(exc), status=400, code="bad_scheme") from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_set_locale(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None, locale: str
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        await set_locale(page, locale)
    except ValueError as exc:
        raise BrowserHandlerError(str(exc), status=400, code="locale_conflict") from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_set_timezone(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None, timezone: str
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        await set_timezone(page, timezone)
    except ValueError as exc:
        raise BrowserHandlerError(str(exc), status=400, code="bad_timezone") from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}


async def handle_set_device(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    descriptor: dict[str, Any],
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        await emulate_device(page, descriptor)
    except (TypeError, KeyError) as exc:
        raise BrowserHandlerError(str(exc), status=400, code="bad_descriptor") from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid}
