"""AgentRouter — gateway-level lazy AgentLoop cache.

Phase 2 of the profile-as-agent multi-routing work. Maps
``profile_id`` to a long-lived ``AgentLoop`` instance. Constructs
each AgentLoop lazily on first inbound; subsequent inbounds for the
same profile reuse the cached instance.

Per-profile-id construction lock (``_build_locks``) prevents two
simultaneous first-inbounds from double-building the same loop.
Broken-profile tracking (``_broken``) is added in Task 2.2.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.gateway.agent_router")


class AgentRouter:
    """Lazy ``{profile_id: AgentLoop}`` cache used by ``Dispatch``.

    Parameters
    ----------
    loop_factory:
        Callable ``(profile_id, profile_home) -> AgentLoop``. Called
        exactly once per profile_id (assuming no broken-profile retry).
    profile_home_resolver:
        Callable ``profile_id -> Path``. Returns the on-disk home
        directory for a given profile_id (typically
        ``~/.opencomputer/<profile_id>``).
    """

    def __init__(
        self,
        *,
        loop_factory: Callable[[str, Path], Any],
        profile_home_resolver: Callable[[str], Path],
    ) -> None:
        self._loop_factory = loop_factory
        self._profile_home_resolver = profile_home_resolver
        self._loops: dict[str, Any] = {}
        self._build_locks: dict[str, asyncio.Lock] = {}

    async def get_or_load(self, profile_id: str) -> Any:
        """Return the cached AgentLoop for ``profile_id``, building one
        on first call. Per-profile-id locking ensures two concurrent
        callers see the same instance."""
        existing = self._loops.get(profile_id)
        if existing is not None:
            return existing

        lock = self._build_locks.setdefault(profile_id, asyncio.Lock())
        async with lock:
            existing = self._loops.get(profile_id)  # double-check
            if existing is not None:
                return existing
            home = self._profile_home_resolver(profile_id)
            loop = self._loop_factory(profile_id, home)
            self._loops[profile_id] = loop
            logger.info("agent_router: built AgentLoop for profile_id=%s", profile_id)
            return loop


__all__ = ["AgentRouter"]
