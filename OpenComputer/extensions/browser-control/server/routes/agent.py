"""Agent routes: navigate / snapshot / screenshot / pdf / act / hooks /
response body / download."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..agent_handlers import (
    handle_act,
    handle_arm_dialog,
    handle_arm_file_chooser,
    handle_download,
    handle_navigate,
    handle_pdf,
    handle_response_body,
    handle_screenshot,
    handle_snapshot,
)
from ._helpers import get_route_ctx, safe_call


def register(app: Any, ctx: Any) -> None:  # noqa: ARG001
    @app.post("/navigate")
    async def navigate(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_navigate(
                c,
                url=b.get("url", ""),
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                timeout_ms=b.get("timeout_ms"),
            )
        )

    @app.post("/snapshot")
    async def snapshot(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_snapshot(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                selector=b.get("selector"),
                frame_selector=b.get("frame_selector"),
                mode=b.get("mode", "role"),
                max_chars=b.get("max_chars"),
                interactive_only=bool(b.get("interactive_only", False)),
                compact=bool(b.get("compact", False)),
            )
        )

    @app.post("/screenshot")
    async def screenshot(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_screenshot(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                full_page=bool(b.get("full_page", False)),
                type=b.get("type", "png"),
            )
        )

    @app.post("/pdf")
    async def pdf(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_pdf(c, profile=b.get("profile"), target_id=b.get("target_id"))
        )

    @app.post("/act")
    async def act(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        kind = b.get("kind")
        if not kind:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=400,
                content={"error": {"message": "kind is required", "code": "ACT_KIND_REQUIRED"}},
            )
        params = b.get("params") if isinstance(b.get("params"), dict) else {
            k: v for k, v in b.items() if k not in ("kind", "profile", "target_id", "params")
        }
        return await safe_call(
            handle_act(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                kind=str(kind),
                params=params,
            )
        )

    @app.post("/hooks/dialog")
    async def hook_dialog(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        if "accept" not in b:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=400,
                content={"error": {"message": "accept is required", "code": "accept_required"}},
            )
        return await safe_call(
            handle_arm_dialog(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                accept=bool(b.get("accept")),
                prompt_text=b.get("prompt_text"),
                timeout_ms=b.get("timeout_ms"),
            )
        )

    @app.post("/hooks/file-chooser")
    async def hook_file_chooser(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        paths = b.get("paths")
        if paths is not None and not isinstance(paths, list):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=400,
                content={"error": {"message": "paths must be a list", "code": "bad_paths"}},
            )
        return await safe_call(
            handle_arm_file_chooser(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                paths=paths,
                timeout_ms=b.get("timeout_ms"),
            )
        )

    @app.post("/response/body")
    async def response_body(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_response_body(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                url=b.get("url", ""),
                timeout_ms=b.get("timeout_ms"),
                max_bytes=b.get("max_bytes"),
                pattern_mode=b.get("pattern_mode", "substring"),
            )
        )

    @app.post("/download")
    async def download(request: Request) -> Any:
        c = get_route_ctx(request)
        b = await _json(request)
        return await safe_call(
            handle_download(
                c,
                profile=b.get("profile"),
                target_id=b.get("target_id"),
                out_dir=b.get("out_dir"),
                out_path=b.get("out_path"),
                timeout_ms=b.get("timeout_ms"),
            )
        )


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}
