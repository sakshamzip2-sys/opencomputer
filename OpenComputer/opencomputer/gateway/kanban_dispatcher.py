"""Kanban dispatcher gateway loop (Wave 6.E.1 / 6.B-β).

Closes the ``oc kanban dispatch`` follow-up: the kanban kernel + tools
landed in PR #429, but spawning sibling worker agents on every
``kanban_create`` event required an always-on dispatcher. Hermes
deprecated the standalone ``kanban daemon`` in favor of an embedded
gateway loop; we mirror that here.

Behaviour:

- On gateway start, read ``cfg.kanban.dispatch_in_gateway`` (default
  true) from the active profile's config.yaml. If false, the loop is
  not started and the user is expected to run ``oc kanban dispatch``
  externally if they want any dispatching at all.
- Tick every ``cfg.kanban.dispatch_interval_seconds`` (default 5).
- Each tick: open the kanban DB, call
  :func:`opencomputer.kanban.db.dispatch_once` with a sane
  ``max_spawn`` cap, log any spawn outcomes.
- Cancellable via the gateway's ``stop()`` path.
- Defensive against transient errors — a failing tick logs + retries;
  it never crashes the gateway.

The actual spawn function is the kanban DB module's ``_default_spawn``
which shells out to ``oc -p <assignee> chat -q "work kanban task <id>"``
with the ``OC_KANBAN_TASK`` env var set. We don't override it — the
default already produces correctly-configured workers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("opencomputer.gateway.kanban_dispatcher")

# Defaults that match hermes-port semantics. Override via
# ``cfg.kanban`` in the active profile's config.yaml.
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_SPAWN = 4
DEFAULT_DISPATCH_IN_GATEWAY = True


class KanbanDispatcherLoop:
    """Periodic ``dispatch_once`` invoker, modeled on OutgoingDrainer.

    One instance lives on :class:`opencomputer.gateway.server.Gateway`
    when kanban dispatch is enabled. Owns the asyncio.Task running
    :meth:`run_forever`; ``stop()`` flips the event and the next tick
    bails out.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        max_spawn: int = DEFAULT_MAX_SPAWN,
    ) -> None:
        self.interval_seconds = float(interval_seconds)
        self.max_spawn = int(max_spawn)
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        """Tick loop. Returns when :meth:`stop` is called."""
        from opencomputer.kanban import db as kdb

        logger.info(
            "kanban dispatcher loop starting (interval=%.1fs, max_spawn=%d)",
            self.interval_seconds, self.max_spawn,
        )
        consecutive_errors = 0
        while not self._stop.is_set():
            try:
                # connect() returns a per-call sqlite3.Connection; the
                # dispatch_once contract owns the txn boundaries
                # internally (release_stale_claims uses write_txn).
                with kdb.connect() as conn:
                    res = kdb.dispatch_once(conn, max_spawn=self.max_spawn)
                if res.spawned or res.crashed or res.timed_out or res.auto_blocked:
                    logger.info(
                        "kanban dispatch: spawned=%d crashed=%d timed_out=%d auto_blocked=%d "
                        "promoted=%d reclaimed=%d",
                        len(res.spawned), len(res.crashed), len(res.timed_out),
                        len(res.auto_blocked), res.promoted, res.reclaimed,
                    )
                    for tid, who, ws in res.spawned:
                        logger.info("kanban spawned: %s -> %s @ %s", tid, who, ws or "-")
                consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001 — fail-open per gateway pattern
                consecutive_errors += 1
                # Backoff to avoid log spam on a persistently broken
                # kanban.db; cap at 60s.
                wait = min(60.0, self.interval_seconds * (2 ** min(consecutive_errors, 4)))
                logger.warning(
                    "kanban dispatch tick failed (#%d, sleeping %.1fs before retry): %s",
                    consecutive_errors, wait, exc,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except TimeoutError:
                    pass
                continue

            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds,
                )
            except TimeoutError:
                pass
        logger.info("kanban dispatcher loop stopped")

    async def stop(self) -> None:
        """Signal the loop to exit on the next iteration."""
        self._stop.set()


def read_kanban_dispatch_config(raw_cfg: dict[str, Any] | None) -> tuple[bool, float, int]:
    """Pull dispatcher tunables from ``config.yaml`` (untyped dict).

    Returns ``(enabled, interval_seconds, max_spawn)``. Missing keys →
    defaults. Unknown types → defaults (fail-open, log a warning at the
    call site if needed).
    """
    if not isinstance(raw_cfg, dict):
        return (DEFAULT_DISPATCH_IN_GATEWAY, DEFAULT_INTERVAL_SECONDS, DEFAULT_MAX_SPAWN)
    block = raw_cfg.get("kanban")
    if not isinstance(block, dict):
        return (DEFAULT_DISPATCH_IN_GATEWAY, DEFAULT_INTERVAL_SECONDS, DEFAULT_MAX_SPAWN)
    enabled = block.get("dispatch_in_gateway", DEFAULT_DISPATCH_IN_GATEWAY)
    if not isinstance(enabled, bool):
        enabled = DEFAULT_DISPATCH_IN_GATEWAY
    interval = block.get("dispatch_interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if not isinstance(interval, (int, float)) or interval <= 0:
        interval = DEFAULT_INTERVAL_SECONDS
    max_spawn = block.get("max_spawn", DEFAULT_MAX_SPAWN)
    if not isinstance(max_spawn, int) or max_spawn <= 0:
        max_spawn = DEFAULT_MAX_SPAWN
    return (bool(enabled), float(interval), int(max_spawn))


__all__ = [
    "KanbanDispatcherLoop",
    "read_kanban_dispatch_config",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAX_SPAWN",
    "DEFAULT_DISPATCH_IN_GATEWAY",
]
