"""Basic routes: status, profiles, start/stop, profile create/delete/reset."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..handlers import (
    handle_known_profile_names,
    handle_list_profiles,
    handle_start,
    handle_status,
    handle_stop,
)
from ._helpers import gate_profile_mutation, get_route_ctx, safe_call


def register(app: Any, ctx: Any) -> None:  # noqa: ARG001
    @app.get("/")
    async def status_root(request: Request, profile: str | None = None) -> Any:
        c = get_route_ctx(request)
        return await safe_call(handle_status(c, profile=profile))

    @app.get("/profiles")
    async def list_profiles(request: Request) -> Any:
        c = get_route_ctx(request)
        return await safe_call(handle_list_profiles(c))

    @app.get("/profile-names")
    async def known_names(request: Request) -> Any:
        c = get_route_ctx(request)
        return await safe_call(handle_known_profile_names(c))

    @app.post("/start")
    async def start(request: Request) -> Any:
        c = get_route_ctx(request)
        body = await _safe_json(request)
        return await safe_call(handle_start(c, profile=body.get("profile")))

    @app.post("/stop")
    async def stop(request: Request) -> Any:
        c = get_route_ctx(request)
        body = await _safe_json(request)
        return await safe_call(handle_stop(c, profile=body.get("profile")))

    @app.post("/profiles/create")
    async def create_profile(request: Request) -> Any:
        c = get_route_ctx(request)
        body = await _safe_json(request)
        return await safe_call(_gated_not_implemented(c, request, body.get("profile")))

    @app.post("/reset-profile")
    async def reset_profile(request: Request) -> Any:
        c = get_route_ctx(request)
        body = await _safe_json(request)
        return await safe_call(_gated_not_implemented(c, request, body.get("profile")))

    @app.delete("/profiles/{name}")
    async def delete_profile(request: Request, name: str) -> Any:
        c = get_route_ctx(request)
        return await safe_call(_gated_not_implemented(c, request, name))


async def _safe_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


async def _gated_not_implemented(c: Any, request: Request, profile_name: str | None) -> Any:
    """Run the profile-mutation gate, then return a 501. Raises through
    safe_call so existing-session profiles get a 403 first."""
    gate_profile_mutation(c, request, profile_name=profile_name)
    from ..handlers import BrowserHandlerError

    raise BrowserHandlerError(
        "not implemented at the HTTP surface yet (W3)",
        status=501,
        code="not_implemented",
    )
