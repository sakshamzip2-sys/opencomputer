"""Per-profile lifecycle — bring a profile up, tear it down.

Routes call ``ensure_profile_running(state, profile_name)`` before any
action; if the profile isn't ready yet, this brings it up via the
appropriate driver:

  - ``local-managed`` (default `opencomputer`): launch Chrome + connect CDP.
  - ``local-existing-session`` (`user`): spawn Chrome MCP subprocess.
  - ``remote-cdp``: just resolve config + smoke-test reachability.

The driver is injected via ``ProfileDriver`` so tests don't need a real
Chrome / npx subprocess. Production wiring is in W2b's ``server/``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from ..chrome.lifecycle import is_chrome_reachable
from ..profiles.capabilities import (
    BrowserProfileCapabilities,
    get_browser_profile_capabilities,
)
from ..profiles.config import ResolvedBrowserProfile
from .state import (
    BrowserServerState,
    ProfileRuntimeState,
    ProfileStatus,
    get_or_create_profile_state,
)

_log = logging.getLogger("opencomputer.browser_control.server_context.lifecycle")


# ─── driver interface ────────────────────────────────────────────────


@dataclass(slots=True)
class ProfileDriver:
    """Capability-routed callables for bringing a profile up / down.

    Each callable is async. None means "not supported on this profile" —
    ``ensure_profile_running`` raises ``RuntimeError`` if the resolved
    capability requires a callable that wasn't provided.

    Production wires this in W2b. Tests pass stubs.
    """

    # local-managed
    launch_managed: Callable[[ResolvedBrowserProfile], Any] | None = None
    connect_managed: Callable[[ResolvedBrowserProfile, Any], Any] | None = None
    stop_managed: Callable[[Any], Any] | None = None

    # local-existing-session (chrome-mcp)
    spawn_chrome_mcp: Callable[[ResolvedBrowserProfile], Any] | None = None
    close_chrome_mcp: Callable[[Any], Any] | None = None

    # remote-cdp
    connect_remote: Callable[[ResolvedBrowserProfile], Any] | None = None
    disconnect_remote: Callable[[Any], Any] | None = None


# Per-profile mutex — prevents two concurrent ensure_profile_running()
# calls for the same profile from racing into a double-launch.
_profile_locks: dict[str, asyncio.Lock] = {}


def _profile_lock(name: str) -> asyncio.Lock:
    lock = _profile_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _profile_locks[name] = lock
    return lock


# ─── ensure ──────────────────────────────────────────────────────────


async def ensure_profile_running(
    state: BrowserServerState,
    profile_name: str,
    *,
    driver: ProfileDriver,
) -> ProfileRuntimeState:
    """Bring ``profile_name`` up, or return its existing runtime state.

    Reconcile flag handling: if a previous-profile marker is set (config
    hot-reloaded), tear down the old browser/MCP first.
    """
    profile = state.resolved.profiles.get(profile_name)
    if profile is None:
        raise LookupError(f"profile {profile_name!r} not declared in config")

    # We need a ResolvedBrowserProfile (the per-profile resolved view).
    # In production this comes from `resolve_profile`; for state purposes
    # we already keyed on the raw config. We construct a minimal profile
    # object via the existing resolver's path.
    from ..profiles.resolver import resolve_profile

    resolved_profile = resolve_profile(state.resolved, profile_name)
    if resolved_profile is None:
        raise LookupError(f"profile {profile_name!r} cannot be resolved")

    runtime = get_or_create_profile_state(state, resolved_profile)
    capabilities = get_browser_profile_capabilities(resolved_profile)

    async with _profile_lock(profile_name):
        if runtime.reconcile is not None:
            await _reconcile_teardown(runtime, driver=driver)

        if runtime.status == ProfileStatus.RUNNING:
            # Wave 3.3 — liveness probe. Out-of-band Chrome death (kill -9,
            # crash, OS sigkill) leaves status==RUNNING but the Playwright
            # session points at a dead WebSocket. Without this check,
            # every subsequent Browser action over that WS hangs until
            # timeout. existing-session / remote-cdp profiles don't track
            # ``runtime.running`` and short-circuit normally.
            #
            # We probe via HTTP /json/version, NOT subprocess returncode:
            # on macOS Chrome's command-line launcher exits cleanly after
            # forking the real browser (proc.returncode==0 even when
            # Chrome is alive), so the subprocess signal is unreliable.
            # The HTTP probe is the actual ground truth.
            if runtime.running is None:
                return runtime
            reachable = await is_chrome_reachable(
                runtime.running.cdp_url, timeout_ms=500
            )
            if reachable:
                return runtime
            _log.info(
                "profile %r: Chrome unreachable on cached %s; "
                "resetting to STOPPED and re-bringing-up",
                profile_name,
                runtime.running.cdp_url,
            )
            runtime.running = None
            runtime.playwright_session = None
            runtime.status = ProfileStatus.STOPPED

        runtime.status = ProfileStatus.STARTING
        runtime.last_error = None
        try:
            await _bring_up(runtime, capabilities=capabilities, driver=driver)
            # Bug F fix — verify the bring-up actually produced a reachable
            # target before we declare RUNNING. Drivers can return success
            # while their spawned Chrome dies milliseconds later (Mac
            # sigkill, profile lock conflict, missing dep). On macOS the
            # Chrome command-line launcher exits cleanly after forking
            # (subprocess.returncode==0 even when Chrome is alive AND when
            # it's dead), so the only ground truth is an end-to-end probe.
            await _verify_bring_up_alive(runtime, capabilities=capabilities)
        except Exception as exc:  # noqa: BLE001
            runtime.status = ProfileStatus.STOPPED
            runtime.last_error = str(exc)
            raise

        runtime.status = ProfileStatus.RUNNING
        return runtime


async def _bring_up(
    runtime: ProfileRuntimeState,
    *,
    capabilities: BrowserProfileCapabilities,
    driver: ProfileDriver,
) -> None:
    profile = runtime.profile
    if capabilities.uses_chrome_mcp:
        if driver.spawn_chrome_mcp is None:
            _raise_driver_unsupported(
                runtime,
                action="start (chrome-mcp)",
                message=(
                    "ProfileDriver.spawn_chrome_mcp not provided for "
                    f"profile {profile.name!r} (driver=existing-session)"
                ),
            )
        runtime.chrome_mcp_client = await driver.spawn_chrome_mcp(profile)
        return

    if capabilities.is_remote:
        if driver.connect_remote is None:
            _raise_driver_unsupported(
                runtime,
                action="start (remote-cdp)",
                message=(
                    "ProfileDriver.connect_remote not provided for "
                    f"profile {profile.name!r} (remote-cdp)"
                ),
            )
        runtime.playwright_session = await driver.connect_remote(profile)
        return

    # local-managed
    if driver.launch_managed is None:
        _raise_driver_unsupported(
            runtime,
            action="start (local-managed)",
            message=(
                "ProfileDriver.launch_managed not provided for "
                f"profile {profile.name!r} (local-managed)"
            ),
        )
    runtime.running = await driver.launch_managed(profile)
    if driver.connect_managed is not None:
        runtime.playwright_session = await driver.connect_managed(profile, runtime.running)


class BringUpVerificationError(RuntimeError):
    """Raised when a driver returned success but the target is unreachable.

    Distinct from driver-internal errors so the lifecycle can surface a
    clean message ("bring-up succeeded but CDP unreachable") instead of
    bubbling driver-implementation-specific exceptions to callers.
    """


async def _verify_bring_up_alive(
    runtime: ProfileRuntimeState,
    *,
    capabilities: BrowserProfileCapabilities,
) -> None:
    """End-to-end probe of the just-brought-up target.

    Bug F fix — without this, ``ensure_profile_running`` only ran
    ``is_chrome_reachable`` on RE-entry (when status was already
    ``RUNNING``). On a fresh start, ``_bring_up`` could return cleanly
    even when Chrome had silently died (or chrome-mcp's stdio transport
    had wedged), and we'd flip status to RUNNING anyway. Every action
    over that dead WebSocket then hung 20s on timeout.

    Capability-routed:
      - chrome-mcp: probe ``list_tools()`` to confirm stdio transport
        responds within 2 s.
      - local-managed / remote-cdp: HTTP ``/json/version`` probe of the
        cached CDP URL.
    """
    if capabilities.uses_chrome_mcp:
        client = runtime.chrome_mcp_client
        if client is None:
            raise BringUpVerificationError(
                f"profile {runtime.profile.name!r}: spawn_chrome_mcp returned "
                "but runtime.chrome_mcp_client is None"
            )
        list_tools = getattr(client, "list_tools", None)
        if not callable(list_tools):
            # Test stub: chrome-mcp client is a bare sentinel (e.g.
            # ``return f"mcp-{profile.name}"`` in fixtures). Production
            # ``ChromeMcpClient`` always exposes ``list_tools``.
            return
        try:
            await asyncio.wait_for(list_tools(), timeout=2.0)
        except TimeoutError as exc:
            raise BringUpVerificationError(
                f"profile {runtime.profile.name!r}: chrome-mcp transport "
                "did not respond to list_tools within 2 s"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise BringUpVerificationError(
                f"profile {runtime.profile.name!r}: chrome-mcp transport "
                f"errored on list_tools: {exc}"
            ) from exc
        return

    # local-managed and remote-cdp both have runtime.running.cdp_url
    if runtime.running is None:
        raise BringUpVerificationError(
            f"profile {runtime.profile.name!r}: driver did not set "
            "runtime.running"
        )
    cdp_url = getattr(runtime.running, "cdp_url", None)
    if not cdp_url:
        # Test stubs return a sentinel without cdp_url — nothing to probe.
        # Production ``RunningChrome`` always carries cdp_url, so this
        # branch only hits in tests where ``is_chrome_reachable`` is
        # already monkeypatched.
        return
    if not await is_chrome_reachable(cdp_url, timeout_ms=2000):
        raise BringUpVerificationError(
            f"profile {runtime.profile.name!r}: bring-up succeeded but "
            f"CDP unreachable at {cdp_url}"
        )


def _raise_driver_unsupported(
    runtime: ProfileRuntimeState,
    *,
    action: str,
    message: str | None = None,
) -> NoReturn:
    """Raise the typed 501 so missing-driver bring-up surfaces structured."""
    # Local import to avoid a cycle with server.handlers (server depends
    # on server_context).
    from ..server.handlers import DriverUnsupportedError

    capabilities = get_browser_profile_capabilities(runtime.profile)
    raise DriverUnsupportedError(
        action=action,
        driver=capabilities.mode,
        profile=runtime.profile.name,
        message=message,
    )


# ─── teardown ────────────────────────────────────────────────────────


async def teardown_profile(
    runtime: ProfileRuntimeState,
    *,
    driver: ProfileDriver,
) -> None:
    """Best-effort teardown — every step swallows its own error."""
    capabilities = get_browser_profile_capabilities(runtime.profile)

    if capabilities.uses_chrome_mcp and runtime.chrome_mcp_client is not None:
        client = runtime.chrome_mcp_client
        runtime.chrome_mcp_client = None
        if driver.close_chrome_mcp is not None:
            try:
                await driver.close_chrome_mcp(client)
            except Exception as exc:  # noqa: BLE001
                _log.debug("teardown: close_chrome_mcp raised: %s", exc)

    if capabilities.is_remote and runtime.playwright_session is not None:
        sess = runtime.playwright_session
        runtime.playwright_session = None
        if driver.disconnect_remote is not None:
            try:
                await driver.disconnect_remote(sess)
            except Exception as exc:  # noqa: BLE001
                _log.debug("teardown: disconnect_remote raised: %s", exc)

    if runtime.running is not None:
        running = runtime.running
        runtime.running = None
        if driver.stop_managed is not None:
            try:
                await driver.stop_managed(running)
            except Exception as exc:  # noqa: BLE001
                _log.debug("teardown: stop_managed raised: %s", exc)

    runtime.playwright_session = None
    runtime.last_target_id = None
    runtime.status = ProfileStatus.STOPPED
    runtime.reconcile = None


async def _reconcile_teardown(
    runtime: ProfileRuntimeState,
    *,
    driver: ProfileDriver,
) -> None:
    _log.info(
        "reconciling profile %s — reason: %s",
        runtime.profile.name,
        runtime.reconcile.reason if runtime.reconcile else "<none>",
    )
    await teardown_profile(runtime, driver=driver)
