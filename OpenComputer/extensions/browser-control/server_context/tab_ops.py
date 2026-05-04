"""Tab operations — open / focus / close.

Each verb dispatches by the profile's resolved capability:

  - ``uses_chrome_mcp`` → call the Chrome MCP wrapper.
  - ``is_remote`` → call the persistent-Playwright wrapper.
  - else (local-managed) → direct CDP /json/ HTTP calls or the Playwright
    session if attached.

The capability-routed callables are passed in via ``TabOpsBackend`` so
this module stays thin and testable without a real Chrome.

``open_tab`` and ``focus_tab`` stamp ``last_target_id``;
``close_tab`` deliberately does not.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NoReturn

from ..profiles.capabilities import get_browser_profile_capabilities
from .selection import select_target_id
from .state import ProfileRuntimeState, TabInfo

_log = logging.getLogger("opencomputer.browser_control.server_context.tab_ops")


@dataclass(slots=True)
class TabOpsBackend:
    """Capability-routed callables for tab operations.

    Each callable is async and returns either a TabInfo (for open),
    a list[TabInfo] (for list), or None (for focus/close).

    Production wires these in W2b. Tests pass in-memory stubs.
    """

    list_tabs: Callable[[ProfileRuntimeState], Awaitable[list[TabInfo]]]
    open_tab_via_mcp: Callable[[ProfileRuntimeState, str], Awaitable[TabInfo]] | None = None
    open_tab_via_playwright: Callable[[ProfileRuntimeState, str], Awaitable[TabInfo]] | None = None
    open_tab_via_cdp: Callable[[ProfileRuntimeState, str], Awaitable[TabInfo]] | None = None
    focus_tab_via_mcp: Callable[[ProfileRuntimeState, str], Awaitable[None]] | None = None
    focus_tab_via_playwright: Callable[[ProfileRuntimeState, str], Awaitable[None]] | None = None
    focus_tab_via_cdp: Callable[[ProfileRuntimeState, str], Awaitable[None]] | None = None
    close_tab_via_mcp: Callable[[ProfileRuntimeState, str], Awaitable[None]] | None = None
    close_tab_via_playwright: Callable[[ProfileRuntimeState, str], Awaitable[None]] | None = None
    close_tab_via_cdp: Callable[[ProfileRuntimeState, str], Awaitable[None]] | None = None


# ─── helpers ─────────────────────────────────────────────────────────


def _driver_label(runtime: ProfileRuntimeState) -> str:
    """Stable human-readable driver label for ``DriverUnsupportedError``."""
    capabilities = get_browser_profile_capabilities(runtime.profile)
    return capabilities.mode


def _raise_driver_unsupported(
    runtime: ProfileRuntimeState, *, action: str, message: str | None = None
) -> NoReturn:
    """Raise the typed 501 error so the agent sees ``driver_unsupported``."""
    # Local import so server_context stays free of server.handlers cycle.
    from ..server.handlers import DriverUnsupportedError

    raise DriverUnsupportedError(
        action=action,
        driver=_driver_label(runtime),
        profile=runtime.profile.name,
        message=message,
    )


def _pick_open_callable(
    runtime: ProfileRuntimeState,
    backend: TabOpsBackend,
) -> Callable[[ProfileRuntimeState, str], Awaitable[TabInfo]]:
    capabilities = get_browser_profile_capabilities(runtime.profile)
    if capabilities.uses_chrome_mcp:
        if backend.open_tab_via_mcp is None:
            _raise_driver_unsupported(
                runtime,
                action="open_tab",
                message=(
                    f"open_tab: no chrome-mcp opener for profile {runtime.profile.name!r}"
                ),
            )
        return backend.open_tab_via_mcp
    if capabilities.uses_persistent_playwright:
        if backend.open_tab_via_playwright is None:
            _raise_driver_unsupported(
                runtime,
                action="open_tab",
                message=(
                    f"open_tab: no playwright opener for profile {runtime.profile.name!r}"
                ),
            )
        return backend.open_tab_via_playwright
    if backend.open_tab_via_cdp is None:
        _raise_driver_unsupported(
            runtime,
            action="open_tab",
            message=f"open_tab: no CDP opener for profile {runtime.profile.name!r}",
        )
    return backend.open_tab_via_cdp


def _pick_focus_callable(
    runtime: ProfileRuntimeState,
    backend: TabOpsBackend,
) -> Callable[[ProfileRuntimeState, str], Awaitable[None]]:
    capabilities = get_browser_profile_capabilities(runtime.profile)
    if capabilities.uses_chrome_mcp:
        if backend.focus_tab_via_mcp is None:
            _raise_driver_unsupported(
                runtime,
                action="focus_tab",
                message=(
                    f"focus_tab: no chrome-mcp focuser for profile {runtime.profile.name!r}"
                ),
            )
        return backend.focus_tab_via_mcp
    if capabilities.uses_persistent_playwright:
        if backend.focus_tab_via_playwright is None:
            _raise_driver_unsupported(
                runtime,
                action="focus_tab",
                message=(
                    f"focus_tab: no playwright focuser for profile {runtime.profile.name!r}"
                ),
            )
        return backend.focus_tab_via_playwright
    if backend.focus_tab_via_cdp is None:
        _raise_driver_unsupported(
            runtime,
            action="focus_tab",
            message=f"focus_tab: no CDP focuser for profile {runtime.profile.name!r}",
        )
    return backend.focus_tab_via_cdp


def _pick_close_callable(
    runtime: ProfileRuntimeState,
    backend: TabOpsBackend,
) -> Callable[[ProfileRuntimeState, str], Awaitable[None]]:
    capabilities = get_browser_profile_capabilities(runtime.profile)
    if capabilities.uses_chrome_mcp:
        if backend.close_tab_via_mcp is None:
            _raise_driver_unsupported(
                runtime,
                action="close_tab",
                message=(
                    f"close_tab: no chrome-mcp closer for profile {runtime.profile.name!r}"
                ),
            )
        return backend.close_tab_via_mcp
    if capabilities.uses_persistent_playwright:
        if backend.close_tab_via_playwright is None:
            _raise_driver_unsupported(
                runtime,
                action="close_tab",
                message=(
                    f"close_tab: no playwright closer for profile {runtime.profile.name!r}"
                ),
            )
        return backend.close_tab_via_playwright
    if backend.close_tab_via_cdp is None:
        _raise_driver_unsupported(
            runtime,
            action="close_tab",
            message=f"close_tab: no CDP closer for profile {runtime.profile.name!r}",
        )
    return backend.close_tab_via_cdp


# ─── verbs ───────────────────────────────────────────────────────────


async def open_tab(
    runtime: ProfileRuntimeState,
    url: str,
    *,
    backend: TabOpsBackend,
) -> TabInfo:
    """Open a new tab. Stamps ``last_target_id`` to the new tab's id."""
    if not url:
        raise ValueError("open_tab: url is empty")
    opener = _pick_open_callable(runtime, backend)
    tab = await opener(runtime, url)
    if not isinstance(tab, TabInfo):
        raise TypeError(f"open_tab callable returned non-TabInfo: {tab!r}")
    runtime.last_target_id = tab.target_id
    return tab


async def focus_tab(
    runtime: ProfileRuntimeState,
    target_id: str | None,
    *,
    backend: TabOpsBackend,
) -> str:
    """Focus a tab. Returns the resolved target id; stamps ``last_target_id``.

    Uses the same selection chain as ``select_target_id`` — pass
    ``target_id=None`` to focus the sticky / first-page tab.
    """
    tabs = await backend.list_tabs(runtime)
    chosen = select_target_id(runtime, tabs=tabs, requested=target_id)
    focuser = _pick_focus_callable(runtime, backend)
    await focuser(runtime, chosen)
    return chosen


async def close_tab(
    runtime: ProfileRuntimeState,
    target_id: str,
    *,
    backend: TabOpsBackend,
) -> None:
    """Close a specific tab. Does NOT stamp ``last_target_id``.

    The deliberate non-stamping mirrors OpenClaw's selection.ts close
    handler — leaves the previous hint in place even though it now points
    to a closed tab. The next ``focus_tab`` call's fallback chain will
    detect "not found" and pick the first ``type=='page'`` tab instead.
    """
    if not target_id:
        raise ValueError("close_tab: target_id is empty")
    closer = _pick_close_callable(runtime, backend)
    await closer(runtime, target_id)
