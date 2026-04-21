"""
Fire-and-forget async runner.

Adapted from kimi-cli's pattern: post-action hooks must NEVER block
the main loop. We schedule them as independent tasks and log any
exceptions (never re-raise).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("opencomputer.hooks.runner")

_pending: set[asyncio.Task[Any]] = set()


def fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Schedule `coro` to run independently. Exceptions are logged, never raised."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running event loop — run synchronously on the current thread as a last resort.
        asyncio.run(coro)
        return
    _pending.add(task)
    task.add_done_callback(_on_done)


def _on_done(task: asyncio.Task[Any]) -> None:
    _pending.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("fire-and-forget hook raised: %s", exc)


__all__ = ["fire_and_forget"]
