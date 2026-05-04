"""Driver entrypoint for the control-extension transport (Wave 6).

Bridges the existing ``ProfileDriver`` interface (in
``server_context/lifecycle.py``) to the new ``ControlDaemon``. The
control-extension transport runs alongside ``managed`` (Playwright)
and ``existing-session`` (chrome-devtools-mcp) — it's not a
replacement; it's the third option.

For v0.6, the daemon is started lazily on first use and shared across
all connected extensions. One daemon per agent process — multiple
profiles can connect their own extensions to the same daemon (each
identified by ``contextId``).

The driver doesn't spawn Chrome itself — Chrome is launched by the
existing ``managed`` driver path with the extension auto-loaded via
``--load-extension`` (Track 1, see ``chrome/launch.py``). The driver
just ensures the daemon is up and ready to receive the extension's
WebSocket connection.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .control_daemon import ControlDaemon
from .control_protocol import DEFAULT_CONTROL_DAEMON_PORT
from .profiles.config import ResolvedBrowserProfile

_log = logging.getLogger("opencomputer.browser_control.control_driver")


# Module-level shared daemon — one per agent process. Lazy-initialized
# on first ``ensure_control_daemon`` call. Kept as a singleton because
# the WS endpoint is bound to a single port; running two daemons on the
# same port would conflict.
_shared_daemon: ControlDaemon | None = None
_daemon_lock = asyncio.Lock()


async def ensure_control_daemon(
    *,
    port: int = DEFAULT_CONTROL_DAEMON_PORT,
) -> ControlDaemon:
    """Return the shared ``ControlDaemon``, starting it if not already up.

    Idempotent — multiple callers under concurrent profile bring-up
    will share the same daemon (mutex-protected init).
    """
    global _shared_daemon
    async with _daemon_lock:
        if _shared_daemon is not None and _shared_daemon._server_task is not None:
            return _shared_daemon
        daemon = ControlDaemon(port=port)
        await daemon.start()
        _shared_daemon = daemon
        _log.info("control_driver: daemon started on ws://127.0.0.1:%d/ext", port)
        return daemon


async def shutdown_control_daemon() -> None:
    """Stop the shared daemon. Idempotent. Best-effort.

    Production callers don't typically invoke this (the agent process
    exits and the daemon dies with it). Tests use this to keep state
    clean across runs.
    """
    global _shared_daemon
    async with _daemon_lock:
        if _shared_daemon is None:
            return
        try:
            await _shared_daemon.stop()
        except Exception as exc:  # noqa: BLE001
            _log.debug("control_driver: shutdown raised: %s", exc)
        _shared_daemon = None


@dataclass(slots=True)
class ControlExtensionClient:
    """Lightweight handle returned to the lifecycle/dispatcher layer.

    Holds a reference to the daemon and the profile's ``contextId`` so
    follow-up commands can target the right extension instance.

    Doesn't own the WS connection — that's owned by ``ControlDaemon``
    on a per-extension basis.
    """

    daemon: ControlDaemon
    context_id: str

    async def list_tools(self) -> list[str]:
        """Compatibility shim for the existing lifecycle liveness probe.

        ``server_context/lifecycle.py:_verify_bring_up_alive`` calls
        ``client.list_tools()`` to confirm the transport is responsive.
        For chrome-mcp this is a real MCP call; for the control
        extension we reflect the supported actions (since the daemon
        has no native ``list_tools`` concept).
        """
        from .control_protocol import SUPPORTED_ACTIONS_V0_6

        return sorted(SUPPORTED_ACTIONS_V0_6)

    async def close(self) -> None:
        """Best-effort detach from the shared daemon.

        Doesn't tear the daemon down — other profiles may still be
        connected. The daemon shuts down on agent exit (or via
        ``shutdown_control_daemon``).
        """
        # Nothing per-client to free yet — connection lifetime is owned
        # by the WS handler in ControlDaemon. Kept as a method for
        # interface symmetry with chrome-mcp's client.close().


async def spawn_browser_control_extension(
    profile: ResolvedBrowserProfile,
) -> ControlExtensionClient:
    """``ProfileDriver``-style entrypoint for the control-extension mode.

    Mirrors the shape of ``snapshot/chrome_mcp.py:spawn_chrome_mcp`` so
    the existing ``ProfileDriver`` interface can route calls here when
    a profile uses ``driver="control-extension"``.

    Note: this does NOT launch Chrome. Chrome is launched by the
    standard ``managed`` driver with ``--load-extension=<dist>`` baked
    in (Track 1, see ``chrome/launch.py``). We just ensure our daemon
    is listening so when the extension boots inside Chrome, it can
    connect.
    """
    daemon = await ensure_control_daemon()
    # contextId here is the OpenComputer profile name — the extension
    # will identify itself by the same string when it connects, so the
    # daemon can route per-profile.
    return ControlExtensionClient(daemon=daemon, context_id=profile.name)


async def close_browser_control_extension(client: ControlExtensionClient) -> None:
    """``ProfileDriver``-style entrypoint for teardown."""
    await client.close()
