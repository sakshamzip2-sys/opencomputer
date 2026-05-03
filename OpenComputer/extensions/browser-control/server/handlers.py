"""Per-route handler implementations + the shared "context" the routes
need (state, auth, ssrf policy, capability-routed backends).

Routes are FastAPI declaratively in ``routes/*.py`` but every handler is
a thin wrapper around one of these implementations. Splitting handler
logic out of the FastAPI router lets the in-process dispatcher unit-test
the same code path without spinning up uvicorn.

Profile-mutation gating (``existing-session`` cannot create profiles
or reset them) lives here in ``ensure_profile_can_mutate`` — called
from any handler that touches the gated paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..profiles.capabilities import get_browser_profile_capabilities
from ..profiles.config import SsrfPolicy
from ..server_context import (
    AmbiguousTargetIdError,
    BrowserServerState,
    ProfileDriver,
    ProfileRuntimeState,
    TabInfo,
    TabNotFoundError,
    ensure_profile_running,
    teardown_profile,
)
from ..server_context import (
    close_tab as ctx_close_tab,
)
from ..server_context import (
    focus_tab as ctx_focus_tab,
)
from ..server_context import (
    open_tab as ctx_open_tab,
)
from ..server_context.state import known_profile_names, list_profile_statuses
from ..server_context.tab_ops import TabOpsBackend
from .auth import BrowserAuth
from .policy import is_persistent_browser_profile_mutation

_log = logging.getLogger("opencomputer.browser_control.server.handlers")


class ProfileMutationDeniedError(PermissionError):
    """The profile (e.g. existing-session) is not allowed to perform this mutation."""


class BrowserHandlerError(RuntimeError):
    """Generic mapped error with an HTTP status hint."""

    def __init__(self, message: str, *, status: int = 500, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ─── route context ───────────────────────────────────────────────────


@dataclass(slots=True)
class BrowserRouteContext:
    """Bundle of dependencies the route handlers need.

    Production wiring (W3) constructs one of these and hands it to
    ``create_app``. Tests construct one with stub backends.
    """

    state: BrowserServerState
    auth: BrowserAuth
    driver: ProfileDriver
    tab_backend: TabOpsBackend
    ssrf_policy: SsrfPolicy | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ─── helpers ─────────────────────────────────────────────────────────


def ensure_profile_can_mutate(profile: Any, *, method: str, path: str) -> None:
    """Raise ProfileMutationDeniedError if this profile can't hit this route."""
    if not is_persistent_browser_profile_mutation(method, path):
        return
    capabilities = get_browser_profile_capabilities(profile)
    if capabilities.uses_chrome_mcp:
        raise ProfileMutationDeniedError(
            f"profile {profile.name!r} cannot mutate persistent browser config "
            f"({method} {path})"
        )


def resolve_profile_name(
    ctx: BrowserRouteContext,
    *,
    query_profile: str | None = None,
    body_profile: str | None = None,
) -> str:
    """Precedence: query → body → default."""
    for cand in (query_profile, body_profile):
        if cand and cand.strip():
            return cand.strip()
    return ctx.state.resolved.default_profile


async def _ensure_running(
    ctx: BrowserRouteContext, profile_name: str
) -> ProfileRuntimeState:
    try:
        return await ensure_profile_running(
            ctx.state, profile_name, driver=ctx.driver
        )
    except LookupError as exc:
        raise BrowserHandlerError(str(exc), status=404, code="profile_not_found") from exc


# ─── basic ───────────────────────────────────────────────────────────


async def handle_status(ctx: BrowserRouteContext, *, profile: str | None) -> dict[str, Any]:
    name = resolve_profile_name(ctx, query_profile=profile)
    runtime = ctx.state.profiles.get(name)
    declared = ctx.state.resolved.profiles.get(name)
    return {
        "enabled": ctx.state.resolved.enabled,
        "evaluate_enabled": ctx.state.resolved.evaluate_enabled,
        "port": ctx.state.port,
        "profile": name,
        "default_profile": ctx.state.resolved.default_profile,
        "running": runtime is not None and runtime.status.value == "running",
        "status": runtime.status.value if runtime else "stopped",
        "last_target_id": runtime.last_target_id if runtime else None,
        "last_error": runtime.last_error if runtime else None,
        "missing_from_config": declared is None,
    }


async def handle_list_profiles(ctx: BrowserRouteContext) -> dict[str, Any]:
    return {"profiles": list_profile_statuses(ctx.state)}


async def handle_known_profile_names(ctx: BrowserRouteContext) -> dict[str, Any]:
    return {"names": known_profile_names(ctx.state)}


async def handle_start(ctx: BrowserRouteContext, *, profile: str | None) -> dict[str, Any]:
    name = resolve_profile_name(ctx, body_profile=profile)
    runtime = await _ensure_running(ctx, name)
    return {"ok": True, "profile": name, "status": runtime.status.value}


async def handle_stop(ctx: BrowserRouteContext, *, profile: str | None) -> dict[str, Any]:
    name = resolve_profile_name(ctx, body_profile=profile)
    runtime = ctx.state.profiles.get(name)
    if runtime is None:
        return {"ok": True, "profile": name, "stopped": False}
    await teardown_profile(runtime, driver=ctx.driver)
    return {"ok": True, "profile": name, "stopped": True}


# ─── tabs ────────────────────────────────────────────────────────────


async def handle_list_tabs(
    ctx: BrowserRouteContext, *, profile: str | None
) -> dict[str, Any]:
    name = resolve_profile_name(ctx, query_profile=profile)
    runtime = await _ensure_running(ctx, name)
    tabs = await ctx.tab_backend.list_tabs(runtime)
    return {
        "running": True,
        "profile": name,
        "tabs": [_tab_to_dict(t) for t in tabs],
    }


async def handle_open_tab(
    ctx: BrowserRouteContext, *, url: str, profile: str | None
) -> dict[str, Any]:
    if not url:
        raise BrowserHandlerError("url is required", status=400, code="url_required")
    name = resolve_profile_name(ctx, body_profile=profile)
    runtime = await _ensure_running(ctx, name)
    tab = await ctx_open_tab(runtime, url, backend=ctx.tab_backend)
    return {"ok": True, "profile": name, "tab": _tab_to_dict(tab)}


async def handle_focus_tab(
    ctx: BrowserRouteContext, *, target_id: str | None, profile: str | None
) -> dict[str, Any]:
    name = resolve_profile_name(ctx, body_profile=profile)
    runtime = await _ensure_running(ctx, name)
    try:
        chosen = await ctx_focus_tab(runtime, target_id, backend=ctx.tab_backend)
    except TabNotFoundError as exc:
        raise BrowserHandlerError(str(exc), status=404, code="tab_not_found") from exc
    except AmbiguousTargetIdError as exc:
        raise BrowserHandlerError(str(exc), status=409, code="tab_ambiguous") from exc
    return {"ok": True, "profile": name, "target_id": chosen}


async def handle_close_tab(
    ctx: BrowserRouteContext, *, target_id: str, profile: str | None
) -> dict[str, Any]:
    if not target_id:
        raise BrowserHandlerError(
            "target_id is required", status=400, code="target_id_required"
        )
    name = resolve_profile_name(ctx, body_profile=profile)
    runtime = await _ensure_running(ctx, name)
    await ctx_close_tab(runtime, target_id, backend=ctx.tab_backend)
    return {"ok": True, "profile": name}


# ─── helpers ─────────────────────────────────────────────────────────


def _tab_to_dict(tab: TabInfo) -> dict[str, Any]:
    return {
        "target_id": tab.target_id,
        "url": tab.url,
        "title": tab.title,
        "type": tab.type,
        "selected": tab.selected,
    }
