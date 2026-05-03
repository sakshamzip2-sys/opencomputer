"""Observation + trace routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..observe_handlers import (
    handle_console,
    handle_debug,
    handle_errors,
    handle_requests,
    handle_trace_start,
    handle_trace_stop,
)
from ._helpers import get_route_ctx, safe_call


def register(app: Any, ctx: Any) -> None:  # noqa: ARG001
    @app.get("/console")
    async def console(
        request: Request,
        profile: str | None = None,
        target_id: str | None = None,
        level: str | None = None,
    ) -> Any:
        c = get_route_ctx(request)
        return await safe_call(
            handle_console(c, profile=profile, target_id=target_id, level=level)
        )

    @app.get("/errors")
    async def errors(
        request: Request,
        profile: str | None = None,
        target_id: str | None = None,
        clear: bool = False,
    ) -> Any:
        c = get_route_ctx(request)
        return await safe_call(
            handle_errors(c, profile=profile, target_id=target_id, clear=clear)
        )

    @app.get("/requests")
    async def requests_(
        request: Request,
        profile: str | None = None,
        target_id: str | None = None,
        filter: str | None = None,
        clear: bool = False,
    ) -> Any:
        c = get_route_ctx(request)
        return await safe_call(
            handle_requests(
                c, profile=profile, target_id=target_id, filter=filter, clear=clear
            )
        )

    @app.post("/trace/start")
    async def trace_start(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_trace_start(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                screenshots=bool(b.get("screenshots", True)),
                snapshots=bool(b.get("snapshots", True)),
                sources=bool(b.get("sources", False)),
            )
        )

    @app.post("/trace/stop")
    async def trace_stop(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_trace_stop(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                path=str(b.get("path", "")),
            )
        )

    @app.get("/debug")
    async def debug(
        request: Request, profile: str | None = None, target_id: str | None = None
    ) -> Any:
        c = get_route_ctx(request)
        return await safe_call(handle_debug(c, profile=profile, target_id=target_id))


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}
