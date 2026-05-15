"""Lease counting for per-session MCP runtimes (Gap F = M4 of the plan).

When :class:`SessionMcpRuntimeManager.sweep_idle` evicts a session,
any in-flight tool call against that session crashes mid-call with a
torn-down session. Lease counting protects the in-flight case:
``MCPTool.execute`` (or any caller) acquires a lease for the duration
of the call; the sweep skips runtimes with active leases.

API:

* :class:`LeaseRegistry` — thread-safe counter keyed by session_id.
* :meth:`LeaseRegistry.acquire(session_id) -> ReleaseFn`. The release
  callable is idempotent — calling it twice doesn't underflow.
* :meth:`LeaseRegistry.acquire_cm(session_id)` — context manager
  wrapper for try/finally-free callers.
* :meth:`LeaseRegistry.has_active_lease(session_id) -> bool`.

This is intentionally NOT a global singleton — :class:`SessionMcpRuntimeManager`
constructs its own and the caller decides whether to share or not.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger("opencomputer.mcp.lease")

#: The shape of the release callable returned by ``acquire``.
ReleaseFn = Callable[[], None]


class LeaseRegistry:
    """Thread-safe per-session lease counter.

    Coarse RLock — contention is negligible because acquire/release
    happens once per MCP tool call, not per turn or per byte.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: dict[str, int] = {}

    def acquire(self, session_id: str) -> ReleaseFn:
        """Increment the lease count for ``session_id``.

        Returns a callable that decrements on call. The release fn is
        idempotent — calling it after the first release is a no-op so
        callers don't have to track whether they've released yet.

        Raises ``ValueError`` on empty session_id (programming error).
        """
        if not session_id:
            raise ValueError("session_id must be non-empty")
        with self._lock:
            self._counts[session_id] = self._counts.get(session_id, 0) + 1
        released = {"done": False}

        def _release() -> None:
            if released["done"]:
                return
            released["done"] = True
            with self._lock:
                current = self._counts.get(session_id, 0)
                if current <= 1:
                    # Drop the key entirely when the count hits zero so
                    # ``has_active_lease`` is True only when somebody
                    # holds a lease.
                    self._counts.pop(session_id, None)
                else:
                    self._counts[session_id] = current - 1

        return _release

    @contextmanager
    def acquire_cm(self, session_id: str) -> Iterator[None]:
        """Context manager wrapper around :meth:`acquire`.

        Releases on exit, INCLUDING the exception path — so callers
        wrapping a tool dispatch get correct lease bookkeeping even
        when the call raises.
        """
        release = self.acquire(session_id)
        try:
            yield
        finally:
            release()

    def has_active_lease(self, session_id: str) -> bool:
        """``True`` iff the count for ``session_id`` is > 0."""
        with self._lock:
            return self._counts.get(session_id, 0) > 0

    def active_leases(self, session_id: str) -> int:
        """Return the current lease count for ``session_id``."""
        with self._lock:
            return self._counts.get(session_id, 0)

    def active_session_ids(self) -> list[str]:
        """List every session_id with at least one active lease."""
        with self._lock:
            return [sid for sid, c in self._counts.items() if c > 0]


__all__ = [
    "LeaseRegistry",
    "ReleaseFn",
]
