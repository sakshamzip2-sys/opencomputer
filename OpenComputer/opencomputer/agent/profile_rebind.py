"""Profile-rebind registry — composition primitive for in-process profile
switching.

Closes the §3 split-brain documented in
``docs/plans/profile-handoff-investigation.md``: after a handoff swap,
multiple subsystems (env var, .env, config, provider, MCP, SessionDB,
browser-profile, ConsentGate) must re-resolve to the new profile.
Without a central registry, each subsystem either binds-once-at-startup
(today's bug) or requires direct knowledge of the others (tight
coupling).

Subsystems register a rebind handler at construction or via the plugin
SDK. ``_apply_pending_profile_swap`` invokes the registry once per
swap, passing ``(new_home, old_home)``. Handlers run in priority order
(lower = earlier) and are exception-isolated — one handler raising
does NOT stop the others, because a partial swap is strictly better
than no swap at all (we already wrote the inbox + sticky file).

Design choices:

* **Order-sensitive.** Env var must rebind BEFORE config (config reads
  ``_home()``). Config before provider/MCP (they read config). The
  registry exposes ``priority: int`` to control order. Convention:
  10-49 = environment, 50-99 = config + providers, 100-149 = stateful
  subsystems (DB / MCP), 150+ = plugins.
* **Exception isolated.** A handler that raises is logged at WARNING
  (with traceback) and its error is surfaced in the per-handler
  ``RebindHandlerResult`` for caller-side inspection (e.g. UI toast
  "browser-profile rebind failed").
* **Sync + async handlers.** Most rebinds are sync (env mutation,
  dict swap); MCP / browser rebinds are async. The registry awaits
  any awaitable returned by the handler.
* **Idempotent re-register.** Registering the same name twice
  replaces the prior handler (plugin reload / test rebuild safety).

See ``docs/plans/profile-handoff-investigation.md`` §9 for the full
list of subsystems and ``docs/superpowers/specs/`` for the parent
implementation plan.
"""
from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


# A rebind handler accepts (new_home, old_home) where old_home may be
# ``None`` on the very first swap of a session. May return None (sync)
# or an awaitable (async).
RebindHandler = Callable[
    [Path, Path | None],
    None | Awaitable[None],
]


@dataclass(frozen=True)
class RebindHandlerResult:
    """Per-handler outcome surfaced from ``ProfileRebindRegistry.invoke``.

    Attributes:
        name: The handler name as registered.
        error: ``None`` on success, the raised exception on failure
            (already logged at WARNING by the registry).
        duration_ms: Wall-clock duration of the handler call,
            including ``await`` time for async handlers.
    """

    name: str
    error: BaseException | None
    duration_ms: float


@dataclass(frozen=True)
class _Entry:
    name: str
    handler: RebindHandler
    priority: int
    registered_at: float


class ProfileRebindRegistry:
    """Ordered, exception-isolated registry of profile-rebind handlers.

    The registry is process-global (one per ``AgentLoop``). Handlers
    are identified by a string name; re-registering an existing name
    replaces the prior handler.

    Thread-safety: the registry is intended for single-threaded
    in-process use (the agent loop). Concurrent registration is not
    supported but registration is rare and almost always happens at
    startup (plugin load, AgentLoop __init__).
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    # ─── registration ─────────────────────────────────────────────

    def register(
        self,
        name: str,
        handler: RebindHandler,
        *,
        priority: int = 100,
    ) -> None:
        """Register a rebind handler under ``name``.

        Args:
            name: Non-empty identifier. Re-registering the same name
                replaces the prior handler (idempotent).
            handler: Callable accepting ``(new_home: Path, old_home: Path | None)``.
                May be sync (returns None) or async (returns awaitable).
            priority: Lower runs earlier. Default 100.

        Raises:
            TypeError: ``handler`` is not callable.
            ValueError: ``name`` is empty.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Handler name must be a non-empty string")
        if not callable(handler):
            raise TypeError("Handler must be callable")
        self._entries[name] = _Entry(
            name=name,
            handler=handler,
            priority=int(priority),
            registered_at=time.monotonic(),
        )

    def unregister(self, name: str) -> bool:
        """Remove a registered handler. Returns ``True`` if it existed."""
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> RebindHandler | None:
        """Return the registered handler for ``name`` or ``None``."""
        entry = self._entries.get(name)
        return entry.handler if entry else None

    @property
    def handler_count(self) -> int:
        return len(self._entries)

    def names(self) -> list[str]:
        """Sorted-by-priority list of handler names. Useful for diagnostics."""
        return [
            e.name
            for e in sorted(
                self._entries.values(),
                key=lambda e: (e.priority, e.registered_at),
            )
        ]

    # ─── invocation ───────────────────────────────────────────────

    async def invoke(
        self,
        new_home: Path,
        old_home: Path | None,
    ) -> list[RebindHandlerResult]:
        """Invoke every registered handler in priority order.

        Each handler is awaited if it returns an awaitable, otherwise
        called synchronously. Exceptions are caught, logged at
        WARNING (with traceback), and recorded in the returned
        per-handler ``RebindHandlerResult`` — they do NOT stop
        subsequent handlers.

        Args:
            new_home: Profile home directory we are switching TO.
                Must be a ``pathlib.Path`` (validated).
            old_home: Profile home we are switching FROM, or ``None``
                if this is the first-ever swap of the process.

        Returns:
            One ``RebindHandlerResult`` per registered handler, in the
            order they were invoked. Empty list if no handlers are
            registered.

        Raises:
            TypeError: if ``new_home`` is not a Path, or ``old_home``
                is provided but is not a Path / None.
        """
        if not isinstance(new_home, Path):
            raise TypeError(
                f"new_home must be a Path, got {type(new_home).__name__}"
            )
        if old_home is not None and not isinstance(old_home, Path):
            raise TypeError(
                f"old_home must be a Path or None, got {type(old_home).__name__}"
            )

        ordered = sorted(
            self._entries.values(),
            key=lambda e: (e.priority, e.registered_at),
        )

        results: list[RebindHandlerResult] = []
        for entry in ordered:
            start = time.monotonic()
            err: BaseException | None = None
            try:
                ret: Any = entry.handler(new_home, old_home)
                if inspect.isawaitable(ret):
                    await ret
            except BaseException as exc:  # noqa: BLE001
                err = exc
                _log.warning(
                    "profile rebind handler %r raised %s — continuing",
                    entry.name,
                    type(exc).__name__,
                    exc_info=True,
                )
                # We still record the failure but do NOT re-raise —
                # other handlers must run so the swap can land
                # partial state cleanly (better than rolling back).
            duration_ms = (time.monotonic() - start) * 1000.0
            results.append(
                RebindHandlerResult(
                    name=entry.name, error=err, duration_ms=duration_ms
                )
            )

        return results

    # ─── debug ─────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"ProfileRebindRegistry(handlers={self.handler_count}, "
            f"names={self.names()})"
        )


def register_mcp_rebind_handler(
    agent_loop: Any,
    mcp_manager: Any,
    *,
    priority: int = 110,
) -> None:
    """Wire an MCP-fleet rebind handler onto an AgentLoop.

    Closes §9.5 of the profile-handoff investigation: on profile swap,
    diff the current ``MCPManager.connections`` against the NEW
    profile's ``config.yaml`` ``mcp.servers`` list — disconnect
    removed servers, connect added/changed ones.

    Call this once at boot (CLI or gateway) AFTER both ``agent_loop``
    and ``mcp_manager`` exist. The handler captures them by reference,
    so a swap mid-session correctly reads through.

    Args:
        agent_loop: The ``AgentLoop`` whose registry will receive the
            handler. Must expose
            ``register_profile_rebind_handler(name, handler, *, priority)``.
        mcp_manager: An ``MCPManager`` instance exposing
            :meth:`diff_cycle`.
        priority: Registry priority. Defaults to 110 — runs AFTER
            ``dotenv`` (20), ``config`` (50), and ``provider`` (60).
    """
    import asyncio  # noqa: F401 — imported for handler scope check

    async def _mcp_diff_cycle(new_home: Path, old_home: Path | None) -> None:  # noqa: ARG001
        # Reload the NEW profile's config and read its mcp.servers
        # list. We don't trust ``agent_loop.config.mcp`` here because
        # ``mcp`` is NOT in the config hot-swap allowlist (it's
        # handled HERE), so agent_loop.config.mcp is still the OLD
        # profile's at this point.
        from opencomputer.agent.config_hot_swap import _load_profile_config

        profile_root = new_home.parent if new_home.name == "home" else new_home
        try:
            new_cfg = _load_profile_config(profile_root)
        except Exception:  # noqa: BLE001 — partial swap is better than nothing
            _log.warning(
                "MCP rebind: failed to load new profile config — skipping "
                "diff_cycle (current MCP fleet retained)",
                exc_info=True,
            )
            return

        servers = list(getattr(new_cfg.mcp, "servers", ()) or ())
        try:
            await mcp_manager.diff_cycle(servers)
        except Exception:  # noqa: BLE001
            _log.warning(
                "MCP rebind: diff_cycle raised — fleet may be partial",
                exc_info=True,
            )

    agent_loop.register_profile_rebind_handler(
        "mcp", _mcp_diff_cycle, priority=priority,
    )


__all__ = [
    "ProfileRebindRegistry",
    "RebindHandler",
    "RebindHandlerResult",
    "register_mcp_rebind_handler",
]
