"""Canonical state shapes for the browser-control orchestrator.

Mirrors OpenClaw's `server-context.types.ts`:

  type ProfileRuntimeState = {
    profile: ResolvedBrowserProfile;
    running: RunningChrome | null;
    lastTargetId?: string | null;
    reconcile?: { previousProfile, reason } | null;
  };

  type BrowserServerState = {
    server?: Server | null;
    port: number;
    resolved: ResolvedBrowserConfig;
    profiles: Map<string, ProfileRuntimeState>;
  };

We add:
  - ``role_refs_by_target`` lives on the ``PlaywrightSession`` for
    local-managed profiles (already in W1a's ``session/``); not duplicated
    here.
  - ``chrome_mcp_client`` slot on ``ProfileRuntimeState`` for the
    chrome-mcp branch — populated by ``ensure_profile_running``.
  - ``status`` enum surfaced via ``profile_status()`` for the
    ``GET /profiles`` route in W2b.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..profiles.config import ResolvedBrowserConfig, ResolvedBrowserProfile


class ProfileStatus(str, Enum):
    """Coarse-grained status for a profile in `BrowserServerState`."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"  # browser running but CDP not reachable / mcp transport torn down


@dataclass(slots=True)
class ReconcileMarker:
    """Set on a ProfileRuntimeState when its config has been hot-reloaded.

    ``ensure_profile_running`` checks this on entry and tears down the
    previous browser/MCP/Playwright state before continuing under the new
    config — see deep-dive §1.10.
    """

    previous_profile: ResolvedBrowserProfile
    reason: str


@dataclass(slots=True)
class TabInfo:
    """One row from a tab listing — capability-uniform across the three drivers."""

    target_id: str
    url: str
    title: str = ""
    type: str = "page"
    selected: bool = False


@dataclass(slots=True)
class ProfileRuntimeState:
    """Per-profile runtime state — what's running and what we've remembered."""

    profile: ResolvedBrowserProfile
    running: Any | None = None  # RunningChrome | None — typed Any to avoid cycle
    last_target_id: str | None = None
    status: ProfileStatus = ProfileStatus.STOPPED
    chrome_mcp_client: Any | None = None  # ChromeMcpClient | None
    control_extension_client: Any | None = None  # ControlExtensionClient | None (Wave 6)
    playwright_session: Any | None = None  # PlaywrightSession | None
    reconcile: ReconcileMarker | None = None
    last_error: str | None = None


@dataclass(slots=True)
class BrowserServerState:
    """Top-level state holder.

    One per running browser-control instance. ``profiles`` is keyed by
    profile name; profile entries are created lazily on first
    ``ensure_profile_running`` call.
    """

    resolved: ResolvedBrowserConfig
    port: int = 0
    profiles: dict[str, ProfileRuntimeState] = field(default_factory=dict)
    server: Any | None = None  # ASGI server / asyncio.Server


# ─── helpers ──────────────────────────────────────────────────────────


def get_or_create_profile_state(
    state: BrowserServerState,
    profile: ResolvedBrowserProfile,
) -> ProfileRuntimeState:
    """Lazy-create a `ProfileRuntimeState` for ``profile.name``.

    If the profile is already known, the cached entry is returned. The
    caller should set ``reconcile`` themselves before calling
    ``ensure_profile_running`` if the profile shape has changed.
    """
    existing = state.profiles.get(profile.name)
    if existing is not None:
        return existing
    runtime = ProfileRuntimeState(profile=profile)
    state.profiles[profile.name] = runtime
    return runtime


def known_profile_names(state: BrowserServerState) -> list[str]:
    """Union of declared (config) and live (Map) names — sorted for determinism."""
    declared = set(state.resolved.profiles.keys())
    live = set(state.profiles.keys())
    return sorted(declared | live)


def list_profile_statuses(state: BrowserServerState) -> list[dict[str, Any]]:
    """One dict per known profile — used by the future `GET /profiles` route."""
    out: list[dict[str, Any]] = []
    for name in known_profile_names(state):
        runtime = state.profiles.get(name)
        declared = state.resolved.profiles.get(name)
        out.append(
            {
                "name": name,
                "status": runtime.status.value if runtime else ProfileStatus.STOPPED.value,
                "last_target_id": runtime.last_target_id if runtime else None,
                "missing_from_config": declared is None,
                "last_error": runtime.last_error if runtime else None,
            }
        )
    return out
