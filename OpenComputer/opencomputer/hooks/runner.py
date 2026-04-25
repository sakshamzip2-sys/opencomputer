"""
Fire-and-forget async runner.

Adapted from kimi-cli's pattern: post-action hooks must NEVER block
the main loop. We schedule them as independent tasks and log any
exceptions (never re-raise).

## Pending-task tracking (Sub-project G.5 — Tier 2.6)

Hooks fired via :func:`fire_and_forget` are tracked in the module-level
``_pending`` set so a graceful shutdown can drain them before the process
exits. Without drain-on-shutdown, the F1 ConsentGate audit chain can
develop gaps when an audit-log hook is mid-flight at SIGTERM time.

Shutdown integration:

- :func:`drain_pending` — async; awaits all currently-pending tasks
  with a bounded timeout (5s default). Cancels any that exceed the
  timeout so the process doesn't hang on a stuck handler.
- :func:`pending_count` — sync; returns how many tasks are in flight.
  Useful for tests / status surfaces.
- The CLI ``atexit`` hook in ``opencomputer/cli.py`` runs
  ``drain_pending`` inside ``asyncio.run`` so audit entries land before
  the process exits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("opencomputer.hooks.runner")

_pending: set[asyncio.Task[Any]] = set()
"""Module-level set of in-flight fire-and-forget tasks.

Each ``fire_and_forget`` call adds the new task; ``_on_done`` discards it
on completion. The set is the source of truth for what shutdown needs to
wait on.
"""

_DEFAULT_DRAIN_TIMEOUT_S = 5.0


def fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Schedule ``coro`` to run independently. Exceptions are logged, never raised.

    The created task is registered in :data:`_pending` so :func:`drain_pending`
    can wait on it during graceful shutdown — F1 audit-log integrity depends on
    this draining before the process exits.
    """
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


def pending_count() -> int:
    """Return the number of in-flight fire-and-forget tasks."""
    return len(_pending)


async def drain_pending(timeout: float = _DEFAULT_DRAIN_TIMEOUT_S) -> tuple[int, int]:
    """Wait for all in-flight fire-and-forget tasks to complete.

    Returns ``(completed, cancelled)`` — number of tasks that finished
    cleanly within the timeout, and number that exceeded the timeout and
    were cancelled. Always returns; never raises.

    The 5-second default balances:

    - F1 audit-log writes are typically sub-millisecond.
    - Telegram notification hooks (network round-trip) can take 1-3 s.
    - We don't want shutdown to hang behind a stuck handler past ~5 s.
    """
    if not _pending:
        return (0, 0)

    # Snapshot so concurrent fire_and_forget calls during drain don't
    # mutate the set we're iterating.
    pending_now = list(_pending)
    logger.info(
        "draining %d fire-and-forget hook(s) (timeout=%.1fs)",
        len(pending_now),
        timeout,
    )

    try:
        done, still_pending = await asyncio.wait(
            pending_now, timeout=timeout, return_when=asyncio.ALL_COMPLETED
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("drain_pending wait raised: %s", exc)
        return (0, 0)

    # Cancel any tasks that didn't finish in time.
    cancelled = 0
    for task in still_pending:
        task.cancel()
        cancelled += 1
        logger.warning(
            "fire-and-forget task exceeded %.1fs drain timeout — cancelled", timeout
        )
    return (len(done), cancelled)


__all__ = [
    "drain_pending",
    "fire_and_forget",
    "pending_count",
]
