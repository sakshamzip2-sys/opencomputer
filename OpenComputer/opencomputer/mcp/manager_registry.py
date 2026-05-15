"""Process-global handle to the active MCPManager (Gap G follow-up).

Late-binding module — keeps the bundle-MCP wakeup machinery decoupled
from any specific manager construction site. ``cli.py`` /
``cli_gateway.py`` call :func:`set_active_manager` after their
MCPManager comes up. The lazy-wakeup stub's ``wakeup_fn`` queries
:func:`current_active_manager` at first-dispatch time to spawn the
underlying bundle.

Symmetric to :mod:`opencomputer.mcp.session_registry` (the active
SessionMcpRuntimeManager handle).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.mcp.client import MCPManager

_lock = threading.RLock()
_active: "MCPManager | None" = None


def set_active_manager(manager: "MCPManager | None") -> None:
    """Install (or clear) the process-global MCPManager handle.

    Pass ``None`` to clear (test fixtures, shutdown). Idempotent;
    safe to call from any thread.
    """
    global _active
    with _lock:
        _active = manager


def current_active_manager() -> "MCPManager | None":
    """Return the active MCPManager, or ``None`` when unset.

    Consumers (lazy-wakeup stubs) handle ``None`` by returning a
    ToolResult error — it means no chat / gateway has bound a manager
    in this process yet.
    """
    with _lock:
        return _active


__all__ = [
    "current_active_manager",
    "set_active_manager",
]
