"""Per-session MCP runtime manager (M2 — mcp-openclaw-port).

Opt-in feature flag (``MCPConfig.session_scoped=True``) scopes one
:class:`opencomputer.mcp.client.MCPManager` instance per session id.
Each instance owns its own background event loop, MCP subprocesses,
and tool registry contributions — so two ``oc chat`` sessions on the
same machine can connect to different MCP server sets without
cross-talk.

Eviction policy:

* **Idle TTL.** Sessions whose ``last_used_at`` is older than
  ``idle_ttl_seconds`` are stopped + dropped on the next sweep
  (callers run :meth:`sweep_idle` from a periodic timer; the manager
  exposes the primitives but doesn't own an asyncio.Task itself so
  callers stay in control of the policy loop).
* **LRU cap.** When the active session count would exceed
  ``max_sessions`` on a new ``get_or_create`` call, the
  least-recently-used session is evicted regardless of its TTL.

Lease-counting is NOT implemented at this milestone — see Milestone 4
of the plan for the opt-in extension. Today, ``sweep_idle`` is best-
effort: a session whose ``last_used_at`` is stale is dropped even if a
tool call is technically in flight, but production callers wrap each
tool dispatch in a :meth:`touch` so any active session's clock is
fresh by definition.

Default OFF. Only relevant for multi-user / multi-tenant deployments
or local setups that legitimately want different MCP server sets per
session (rare).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencomputer.mcp.client import MCPManager

logger = logging.getLogger("opencomputer.mcp.session_runtime")


@dataclass(frozen=True, slots=True)
class SessionMcpRuntimeStats:
    """Read-only snapshot of one session runtime's bookkeeping.

    Surfaced by ``oc mcp sessions`` so operators can see what's alive.
    """

    session_id: str
    created_at: float
    last_used_at: float
    connection_count: int


@dataclass(slots=True)
class _Slot:
    """Internal bookkeeping entry — one per active session."""

    manager: MCPManager
    created_at: float
    last_used_at: float


class SessionMcpRuntimeManager:
    """Process-global registry of per-session MCP managers.

    Construction is cheap — no asyncio resources allocated until a
    session actually arrives via :meth:`get_or_create`. Coarse RLock
    serialises every public method; contention is negligible because
    per-session calls are rare (one per session lifecycle, not per
    tool call).

    The actual MCPManager construction is delegated to a factory the
    caller passes at construction time. The factory should produce a
    fresh, unconfigured :class:`opencomputer.mcp.client.MCPManager`
    bound to whatever tool registry / dependencies the caller wants;
    the session-runtime layer only owns lifecycle.
    """

    def __init__(
        self,
        mcp_manager_factory: Callable[[], MCPManager],
        *,
        idle_ttl_seconds: float = 300.0,
        max_sessions: int = 20,
    ) -> None:
        self._factory = mcp_manager_factory
        self.idle_ttl_seconds = float(idle_ttl_seconds)
        self.max_sessions = int(max_sessions)
        self._lock = threading.RLock()
        self._slots: dict[str, _Slot] = {}

    # ── lookup / creation ──────────────────────────────────────────

    def get_or_create(self, session_id: str) -> MCPManager:
        """Return the manager for ``session_id``, creating one if absent.

        Side effects:

        * Constructs a new manager via the factory on first call.
        * Updates ``last_used_at`` to the current monotonic-ish wall time.
        * If the active session count would exceed ``max_sessions``,
          evicts the LRU session first.
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        now = time.time()
        with self._lock:
            slot = self._slots.get(session_id)
            if slot is not None:
                slot.last_used_at = now
                return slot.manager
            # New session — enforce LRU cap BEFORE constructing.
            if len(self._slots) >= self.max_sessions:
                self._evict_lru_locked()
            manager = self._factory()
            self._slots[session_id] = _Slot(
                manager=manager,
                created_at=now,
                last_used_at=now,
            )
            logger.info(
                "session MCP runtime created: session_id=%s (active=%d)",
                session_id, len(self._slots),
            )
            return manager

    def touch(self, session_id: str) -> bool:
        """Refresh the ``last_used_at`` timestamp for ``session_id``.

        Returns ``True`` if the session exists, ``False`` otherwise.
        Callers (the AgentLoop) call this around each tool dispatch so
        the idle-TTL sweep doesn't evict actively-used sessions.
        """
        now = time.time()
        with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                return False
            slot.last_used_at = now
            return True

    # ── eviction ──────────────────────────────────────────────────

    def dispose(self, session_id: str) -> bool:
        """Stop + drop the runtime for ``session_id``.

        Returns ``True`` if the session was active, ``False`` otherwise.
        Best-effort: a failure inside the manager's stop path is logged
        but the slot is still dropped.
        """
        with self._lock:
            slot = self._slots.pop(session_id, None)
            if slot is None:
                return False
        self._stop_manager_safely(session_id, slot.manager)
        return True

    def dispose_all(self) -> int:
        """Stop every active session — returns the count disposed.

        Use at process shutdown. Safe to call multiple times (idempotent).
        """
        with self._lock:
            slots = list(self._slots.items())
            self._slots.clear()
        for sid, slot in slots:
            self._stop_manager_safely(sid, slot.manager)
        return len(slots)

    def sweep_idle(self) -> list[str]:
        """Evict sessions whose ``last_used_at`` is older than the TTL.

        Returns the list of session ids that were evicted. Designed
        to be called from a periodic timer (every ~60s in production).
        """
        now = time.time()
        cutoff = now - self.idle_ttl_seconds
        evicted: list[tuple[str, MCPManager]] = []
        with self._lock:
            for sid in list(self._slots.keys()):
                slot = self._slots[sid]
                if slot.last_used_at < cutoff:
                    evicted.append((sid, slot.manager))
                    del self._slots[sid]
        for sid, mgr in evicted:
            logger.info(
                "session MCP runtime evicted (idle): session_id=%s", sid,
            )
            self._stop_manager_safely(sid, mgr)
        return [sid for sid, _ in evicted]

    def _evict_lru_locked(self) -> None:
        """Internal — drop the least-recently-used slot. Caller holds the lock."""
        if not self._slots:
            return
        lru_sid = min(
            self._slots.keys(),
            key=lambda k: self._slots[k].last_used_at,
        )
        slot = self._slots.pop(lru_sid)
        logger.info(
            "session MCP runtime evicted (LRU): session_id=%s (cap=%d)",
            lru_sid, self.max_sessions,
        )
        # Stop happens outside the lock; defer via temp list isn't
        # needed here because we only have one item — but we still
        # call stop OUTSIDE the lock by releasing/re-acquiring. The
        # implementation is "stop while holding lock" for simplicity:
        # MCPManager.stop_background_loop is fast (joins a thread with
        # a 5s timeout) and held-lock duration is bounded.
        self._stop_manager_safely(lru_sid, slot.manager)

    def _stop_manager_safely(
        self, session_id: str, manager: MCPManager,
    ) -> None:
        """Stop a manager + swallow + log any error.

        We never raise out of eviction — a wedged manager must not
        prevent the registry from honouring further requests.
        """
        try:
            manager.stop_background_loop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session MCP runtime %s shutdown raised: %s — slot still dropped",
                session_id, exc,
            )

    # ── introspection ─────────────────────────────────────────────

    def active_session_ids(self) -> list[str]:
        """List session ids that currently own a runtime."""
        with self._lock:
            return list(self._slots.keys())

    def stats_for_session(
        self, session_id: str,
    ) -> SessionMcpRuntimeStats | None:
        """Return a read-only snapshot of one session's bookkeeping."""
        with self._lock:
            slot = self._slots.get(session_id)
            if slot is None:
                return None
            conn_count = len(getattr(slot.manager, "connections", []) or [])
            return SessionMcpRuntimeStats(
                session_id=session_id,
                created_at=slot.created_at,
                last_used_at=slot.last_used_at,
                connection_count=conn_count,
            )

    def stats_all(self) -> list[SessionMcpRuntimeStats]:
        """Snapshot every active session's bookkeeping for CLI / metrics."""
        with self._lock:
            ids = list(self._slots.keys())
        out: list[SessionMcpRuntimeStats] = []
        for sid in ids:
            s = self.stats_for_session(sid)
            if s is not None:
                out.append(s)
        return out


def build_default_factory(tool_registry: Any) -> Callable[[], MCPManager]:
    """Build the production factory that returns a fresh :class:`MCPManager`.

    Late-imports :mod:`opencomputer.mcp.client` so the session-runtime
    module's import surface stays narrow (the runtime can be discussed
    in plans + tests without dragging in the heavy MCP client).
    """
    def _factory() -> MCPManager:
        from opencomputer.mcp.client import MCPManager
        return MCPManager(tool_registry=tool_registry)
    return _factory


__all__ = [
    "SessionMcpRuntimeManager",
    "SessionMcpRuntimeStats",
    "build_default_factory",
]
