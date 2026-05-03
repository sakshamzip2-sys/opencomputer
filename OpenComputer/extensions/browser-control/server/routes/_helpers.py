"""Shared helpers for route modules.

Most routes:

  1. Read ``profile`` from query (GET) or body (POST/PUT/DELETE).
  2. Validate ``existing-session`` can't hit profile-mutation routes.
  3. Call into the matching handler in ``server.handlers`` /
     ``agent_handlers`` / ``storage_handlers`` / ``observe_handlers``.
  4. Map ``BrowserHandlerError.status`` → JSONResponse.
  5. Map ``ProfileMutationDeniedError`` → 403.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..handlers import (
    BrowserHandlerError,
    BrowserRouteContext,
    ProfileMutationDeniedError,
    ensure_profile_can_mutate,
    resolve_profile_name,
)


async def safe_call(coro: Any) -> Any:
    """Run ``coro``, catching handler errors → JSONResponse."""
    try:
        result = await coro
    except ProfileMutationDeniedError as exc:
        return JSONResponse(
            status_code=403,
            content={"error": {"message": str(exc), "code": "profile_mutation_denied"}},
        )
    except BrowserHandlerError as exc:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"message": str(exc), "code": exc.code or "error"}},
        )
    except LookupError as exc:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": str(exc), "code": "not_found"}},
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": str(exc), "code": "invalid"}},
        )
    return result


def gate_profile_mutation(ctx: BrowserRouteContext, request: Request, *, profile_name: str | None) -> None:
    """Enforce ``existing-session`` can't hit mutation routes."""
    name = profile_name or ctx.state.resolved.default_profile
    declared = ctx.state.resolved.profiles.get(name)
    if declared is None:
        return  # let the handler 404 with a better message
    runtime = ctx.state.profiles.get(name)
    profile = runtime.profile if runtime is not None else None
    if profile is None:
        # Resolve a transient profile shape for capability check
        from ...profiles.resolver import resolve_profile  # type: ignore[import-not-found]

        profile = resolve_profile(ctx.state.resolved, name)
    if profile is None:
        return
    ensure_profile_can_mutate(profile, method=request.method, path=request.url.path)


def get_route_ctx(request: Request) -> BrowserRouteContext:
    """Pull the BrowserRouteContext stashed on app.state."""
    ctx = getattr(request.app.state, "browser_ctx", None)
    if not isinstance(ctx, BrowserRouteContext):
        raise BrowserHandlerError(
            "BrowserRouteContext not configured on app.state", status=500
        )
    return ctx


__all__ = [
    "gate_profile_mutation",
    "get_route_ctx",
    "resolve_profile_name",
    "safe_call",
]
