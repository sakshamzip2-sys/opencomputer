"""Tab routes: list / open / focus / close."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..handlers import handle_close_tab, handle_focus_tab, handle_list_tabs, handle_open_tab
from ._helpers import get_route_ctx, safe_call


def register(app: Any, ctx: Any) -> None:  # noqa: ARG001
    @app.get("/tabs")
    async def list_tabs(request: Request, profile: str | None = None) -> Any:
        c = get_route_ctx(request)
        return await safe_call(handle_list_tabs(c, profile=profile))

    @app.post("/tabs/open")
    async def open_tab(request: Request) -> Any:
        c = get_route_ctx(request)
        body = await _json(request)
        return await safe_call(
            handle_open_tab(c, url=body.get("url", ""), profile=body.get("profile"))
        )

    @app.post("/tabs/focus")
    async def focus_tab(request: Request) -> Any:
        c = get_route_ctx(request)
        body = await _json(request)
        return await safe_call(
            handle_focus_tab(c, target_id=body.get("target_id"), profile=body.get("profile"))
        )

    @app.delete("/tabs/{target_id}")
    async def close_tab(request: Request, target_id: str, profile: str | None = None) -> Any:
        c = get_route_ctx(request)
        return await safe_call(
            handle_close_tab(c, target_id=target_id, profile=profile)
        )


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}
