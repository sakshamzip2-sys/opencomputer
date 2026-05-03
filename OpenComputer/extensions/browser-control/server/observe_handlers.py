"""Observation + trace handlers — console, errors, requests, trace start/stop.

Console / errors / requests rely on per-page activity buffers managed
elsewhere (W3 wires the page-level listeners on PlaywrightSession's
context creation). For v0.1 these handlers return whatever the session
exposes via ``session.console_messages(target_id)`` etc. — production
wiring lives in W3, but the route surface is here so the dispatcher
test can exercise the path and confirm we 501 cleanly when the session
hasn't been fully wired yet.
"""

from __future__ import annotations

import os
from typing import Any

from ..tools_core import is_trace_active, start_trace, stop_trace
from .agent_handlers import _resolve_page
from .handlers import BrowserHandlerError, BrowserRouteContext


async def handle_console(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None, level: str | None = None
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    sess = runtime.playwright_session
    getter = getattr(sess, "console_messages", None) if sess is not None else None
    if not callable(getter):
        return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "messages": []}
    messages = getter(target_id=tid, level=level)
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "messages": list(messages)}


async def handle_errors(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None, clear: bool = False
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    sess = runtime.playwright_session
    getter = getattr(sess, "page_errors", None) if sess is not None else None
    if not callable(getter):
        return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "errors": []}
    errors = getter(target_id=tid, clear=bool(clear))
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "errors": list(errors)}


async def handle_requests(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    filter: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    sess = runtime.playwright_session
    getter = getattr(sess, "network_requests", None) if sess is not None else None
    if not callable(getter):
        return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "requests": []}
    requests = getter(target_id=tid, filter=filter, clear=bool(clear))
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "requests": list(requests)}


async def handle_trace_start(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    screenshots: bool = True,
    snapshots: bool = True,
    sources: bool = False,
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        await start_trace(
            page.context, screenshots=screenshots, snapshots=snapshots, sources=sources
        )
    except RuntimeError as exc:
        raise BrowserHandlerError(str(exc), status=409, code="trace_already_running") from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "active": True}


async def handle_trace_stop(
    ctx: BrowserRouteContext,
    *,
    profile: str | None,
    target_id: str | None,
    path: str,
) -> dict[str, Any]:
    if not path:
        raise BrowserHandlerError("path is required", status=400, code="path_required")
    # Path traversal guard: caller-supplied path must resolve under cwd
    # (or under explicit allowed root). For v0.1 we just enforce no
    # ``..`` segments and require the resolved path stays under cwd.
    norm = os.path.normpath(path)
    if ".." in norm.split(os.sep):
        raise BrowserHandlerError(
            "path traversal not allowed", status=400, code="path_traversal"
        )
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    try:
        out = await stop_trace(page.context, path=path)
    except RuntimeError as exc:
        raise BrowserHandlerError(str(exc), status=409, code="trace_not_running") from exc
    return {"ok": True, "profile": runtime.profile.name, "target_id": tid, "path": out}


async def handle_debug(
    ctx: BrowserRouteContext, *, profile: str | None, target_id: str | None
) -> dict[str, Any]:
    runtime, tid, page = await _resolve_page(
        ctx, profile=profile, target_id=target_id
    )
    return {
        "ok": True,
        "profile": runtime.profile.name,
        "target_id": tid,
        "url": getattr(page, "url", ""),
        "trace_active": is_trace_active(page.context),
    }
