"""Process-global handle to the active :class:`SessionMcpRuntimeManager`.

Tiny indirection module — keeps the binding logic out of
:mod:`opencomputer.mcp.session_runtime` so the runtime module remains a
pure data type and the *binding* (production-only side effect) can be
swapped or zeroed in tests.

mcp-openclaw-port M2 (2026-05-15). The CLI (`oc mcp sessions`) and any
operational tools that need to introspect active per-session runtimes
look up the manager here. The AgentLoop (or its bootstrap path)
installs the live manager via :func:`set_runtime_manager` when
``MCPConfig.session_scoped=True``; otherwise the slot stays ``None``
and consumers handle the empty case explicitly.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.mcp.session_runtime import SessionMcpRuntimeManager

_lock = threading.RLock()
_active: SessionMcpRuntimeManager | None = None


def set_runtime_manager(
    manager: SessionMcpRuntimeManager | None,
) -> None:
    """Install (or clear) the process-global runtime manager.

    Pass ``None`` to clear (test fixtures, shutdown). Idempotent; safe
    to call from any thread.
    """
    global _active
    with _lock:
        _active = manager


def current_runtime_manager() -> SessionMcpRuntimeManager | None:
    """Return the active runtime manager, or ``None`` when unset.

    Consumers (the CLI) should handle ``None`` explicitly — it means
    no agent in this process has opted in to session-scoped runtimes.
    """
    with _lock:
        return _active


__all__ = [
    "current_runtime_manager",
    "set_runtime_manager",
]
