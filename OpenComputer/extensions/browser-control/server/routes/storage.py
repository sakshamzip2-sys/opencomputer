"""Storage + emulation routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..storage_handlers import (
    handle_clear_cookies,
    handle_get_cookies,
    handle_set_cookie,
    handle_set_credentials,
    handle_set_device,
    handle_set_geolocation,
    handle_set_headers,
    handle_set_locale,
    handle_set_media,
    handle_set_offline,
    handle_set_timezone,
    handle_storage_clear,
    handle_storage_get,
    handle_storage_remove,
    handle_storage_set,
)
from ._helpers import get_route_ctx, safe_call


def register(app: Any, ctx: Any) -> None:  # noqa: ARG001
    @app.get("/cookies")
    async def get_cookies(
        request: Request, profile: str | None = None, target_id: str | None = None
    ) -> Any:
        c = get_route_ctx(request)
        return await safe_call(handle_get_cookies(c, profile=profile, target_id=target_id))

    @app.post("/cookies/set")
    async def set_cookie(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        cookie = b.get("cookie")
        if not isinstance(cookie, dict):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=400,
                content={"error": {"message": "cookie is required", "code": "cookie_required"}},
            )
        return await safe_call(
            handle_set_cookie(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                cookie=cookie,
            )
        )

    @app.post("/cookies/clear")
    async def clear_cookies(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_clear_cookies(c, profile=b.get("profile"), target_id=b.get("target_id"))
        )

    @app.get("/storage/{kind}")
    async def storage_get(
        request: Request,
        kind: str,
        profile: str | None = None,
        target_id: str | None = None,
        key: str | None = None,
    ) -> Any:
        c = get_route_ctx(request)
        return await safe_call(
            handle_storage_get(c, profile=profile, target_id=target_id, kind=kind, key=key)
        )

    @app.post("/storage/{kind}/set")
    async def storage_set(request: Request, kind: str) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_storage_set(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                kind=kind,
                key=str(b.get("key", "")),
                value=str(b.get("value", "")),
            )
        )

    @app.post("/storage/{kind}/remove")
    async def storage_remove(request: Request, kind: str) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_storage_remove(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                kind=kind,
                key=str(b.get("key", "")),
            )
        )

    @app.post("/storage/{kind}/clear")
    async def storage_clear(request: Request, kind: str) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_storage_clear(
                c, profile=b.get("profile"), target_id=b.get("target_id"), kind=kind
            )
        )

    @app.post("/set/offline")
    async def set_offline(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_offline(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                offline=bool(b.get("offline")),
            )
        )

    @app.post("/set/headers")
    async def set_headers(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_headers(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                headers=b.get("headers") or {},
            )
        )

    @app.post("/set/credentials")
    async def set_credentials(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_credentials(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                username=b.get("username"),
                password=b.get("password"),
                clear=bool(b.get("clear", False)),
            )
        )

    @app.post("/set/geolocation")
    async def set_geo(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_geolocation(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                latitude=b.get("latitude"),
                longitude=b.get("longitude"),
                accuracy=b.get("accuracy"),
                clear=bool(b.get("clear", False)),
            )
        )

    @app.post("/set/media")
    async def set_media(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_media(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                color_scheme=str(b.get("color_scheme", "light")),
            )
        )

    @app.post("/set/locale")
    async def set_locale(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_locale(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                locale=str(b.get("locale", "")),
            )
        )

    @app.post("/set/timezone")
    async def set_tz(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_timezone(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                timezone=str(b.get("timezone", "")),
            )
        )

    @app.post("/set/device")
    async def set_device(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_set_device(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                descriptor=b.get("descriptor") or {},
            )
        )


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}
