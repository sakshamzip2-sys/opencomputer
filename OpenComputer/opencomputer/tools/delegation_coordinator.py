"""Delegation coordinator — per-path async locks for concurrent sibling subagents.

Mirrors hermes-agent v0.11's file-coordination layer (PRs #13691, #13718).
When multiple sibling delegates run concurrently, this module gives each
one an asyncio.Lock keyed by absolute file path. Sorted-path acquisition
prevents deadlock (lock A → lock B vs B → A); per-acquisition timeout
fail-fast prevents indefinite hangs.

Usage::

    coord = get_default_coordinator()
    async with coord.acquire_paths(["/proj/a.py", "/proj/b.py"]) as locks:
        # safe to write to a.py and b.py
        ...

Thread-safety: a single asyncio event loop assumed (subagents run on
the parent's loop). For cross-thread usage, the registry's lock dict
is itself protected by an asyncio.Lock.

PR-E of ~/.claude/plans/replicated-purring-dewdrop.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT_SECONDS: float = 30.0
"""How long a delegate waits for a path lock before failing fast."""


class DelegationLockTimeout(TimeoutError):  # noqa: N818
    """Raised when a delegate can't acquire all path locks within timeout."""


class DelegationCoordinator:
    """Process-wide per-path async lock registry.

    Each absolute path gets one asyncio.Lock. Multiple acquirers wait in
    FIFO order. Sorted-path acquisition (when acquiring multiple paths
    simultaneously) prevents deadlock between siblings.
    """

    def __init__(self, *, lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock: asyncio.Lock = asyncio.Lock()
        self._lock_timeout: float = lock_timeout_seconds

    def _normalize(self, path: str | Path) -> str:
        """Absolute path string; case-preserving on macOS, normalized separators."""
        return os.path.abspath(str(path))

    async def _get_or_create_lock(self, abs_path: str) -> asyncio.Lock:
        """Find or create the lock for a path. Registry mutation guarded."""
        async with self._registry_lock:
            lock = self._locks.get(abs_path)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[abs_path] = lock
            return lock

    @asynccontextmanager
    async def acquire_paths(self, paths: Sequence[str | Path]) -> AsyncIterator[list[str]]:
        """Acquire locks for all `paths` in sorted order. Release on exit.

        Yields the list of normalized absolute paths actually locked.

        Raises DelegationLockTimeout if any path lock can't be acquired
        within `lock_timeout_seconds`. Already-acquired locks are released
        before raising so partial-acquisition deadlocks are impossible.

        Empty paths list = no-op (yields []).
        """
        if not paths:
            yield []
            return

        # Sort by normalized path → consistent acquisition order across
        # all callers prevents A→B / B→A deadlock between siblings.
        normalized = sorted({self._normalize(p) for p in paths})
        acquired: list[asyncio.Lock] = []
        try:
            for abs_path in normalized:
                lock = await self._get_or_create_lock(abs_path)
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=self._lock_timeout)
                except TimeoutError as exc:
                    # Release everything acquired so far before raising
                    raise DelegationLockTimeout(
                        f"Could not acquire lock on {abs_path} within "
                        f"{self._lock_timeout}s (held by another sibling delegate?)"
                    ) from exc
                acquired.append(lock)
            yield normalized
        finally:
            # Release in reverse order (matches typical lock-stack discipline)
            for lock in reversed(acquired):
                if lock.locked():
                    lock.release()

    def stats(self) -> dict:
        """Diagnostic snapshot — count of registered locks + held vs free."""
        held = sum(1 for lock in self._locks.values() if lock.locked())
        return {
            "total_paths_registered": len(self._locks),
            "currently_held": held,
            "currently_free": len(self._locks) - held,
            "lock_timeout_seconds": self._lock_timeout,
        }


# Module-level default coordinator — DelegateTool uses this unless overridden
_default_coordinator: DelegationCoordinator | None = None


def get_default_coordinator() -> DelegationCoordinator:
    """Return (lazy-init) the process-wide default coordinator."""
    global _default_coordinator
    if _default_coordinator is None:
        _default_coordinator = DelegationCoordinator()
    return _default_coordinator


def reset_default_coordinator() -> None:
    """Test helper — clear the singleton between tests."""
    global _default_coordinator
    _default_coordinator = None


__all__ = [
    "DelegationCoordinator",
    "DelegationLockTimeout",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "get_default_coordinator",
    "reset_default_coordinator",
]
