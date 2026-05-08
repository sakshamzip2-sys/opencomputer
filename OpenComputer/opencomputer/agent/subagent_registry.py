"""SubagentRegistry — in-process tracker for active subagents.

Backs ``oc agents list/kill/history``. Best-effort: cross-process
subagents (none in the current architecture) wouldn't appear here.

Hermes parity (2026-05-08): closes the operational gap of "what's
running right now and how do I cancel it" without requiring a full
TUI overlay (deferred).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class SubagentRecord:
    """One row in the registry. A subagent's lifecycle.

    ``state`` transitions: running → completed | failed | killed.
    ``cancel_event`` and ``event_loop`` are set on register; the kill()
    path uses ``event_loop.call_soon_threadsafe(cancel_event.set)`` so
    cancellation crosses the loop boundary safely (F4 audit fix from
    the 2026-05-08 plan-audit).
    """

    agent_id: str
    parent_id: str | None
    goal: str
    started_at: datetime
    ended_at: datetime | None = None
    current_tool: str | None = None
    tokens_used: int = 0
    state: str = "running"  # running | completed | failed | killed
    error: str | None = None
    cancel_event: asyncio.Event | None = None
    event_loop: asyncio.AbstractEventLoop | None = None


class SubagentRegistry:
    """Thread-safe singleton tracking subagent lifecycles."""

    _instance: SubagentRegistry | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._records: dict[str, SubagentRecord] = {}
        self._records_lock = threading.RLock()

    @classmethod
    def instance(cls) -> SubagentRegistry:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def register(
        self,
        *,
        parent_id: str | None,
        goal: str,
        event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> SubagentRecord:
        """Add a running subagent record. Returns the record (for
        ``update`` / ``kill`` callbacks).

        ``event_loop`` defaults to the running loop at the time of call —
        captures the child's loop so cross-thread kill via
        ``call_soon_threadsafe`` works.
        """
        if event_loop is None:
            try:
                event_loop = asyncio.get_running_loop()
            except RuntimeError:
                event_loop = None
        rec = SubagentRecord(
            agent_id=f"sub-{uuid.uuid4().hex[:10]}",
            parent_id=parent_id,
            goal=goal[:200],
            started_at=datetime.now(UTC),
            cancel_event=asyncio.Event() if event_loop is not None else None,
            event_loop=event_loop,
        )
        with self._records_lock:
            self._records[rec.agent_id] = rec
        return rec

    def update(self, agent_id: str, **fields: Any) -> None:
        """Mutate a record's fields. Silently no-ops on unknown id."""
        with self._records_lock:
            rec = self._records.get(agent_id)
            if rec is None:
                return
            for k, v in fields.items():
                if hasattr(rec, k):
                    setattr(rec, k, v)

    def kill(self, agent_id: str) -> bool:
        """Signal a running subagent to cancel.

        Returns True if the kill signal was dispatched (the subagent was
        running); False if the agent doesn't exist or has already
        finished. The caller's loop NEVER blocks on the child's exit.
        """
        with self._records_lock:
            rec = self._records.get(agent_id)
            if rec is None or rec.state != "running":
                return False
            rec.state = "killed"
            rec.ended_at = datetime.now(UTC)
            ev = rec.cancel_event
            child_loop = rec.event_loop

        # F4 audit fix: asyncio.Event.set is not safe across event loops.
        # Use call_soon_threadsafe to deliver the set to the child's loop.
        if ev is not None and child_loop is not None:
            try:
                child_loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                # Child loop already closed — record state already 'killed'
                # so list/history reflect it. Nothing else to do.
                pass
        return True

    def list_running(self) -> list[SubagentRecord]:
        with self._records_lock:
            return [r for r in self._records.values() if r.state == "running"]

    def history(self, *, limit: int = 50) -> list[SubagentRecord]:
        """Most recent N completed/failed/killed records (newest first)."""
        with self._records_lock:
            ended = [r for r in self._records.values() if r.state != "running"]
        # Default to oldest-equal order if ended_at is missing for some reason.
        epoch = datetime.fromtimestamp(0, tz=UTC)
        ended.sort(key=lambda r: r.ended_at or epoch, reverse=True)
        return ended[:limit]

    def reset(self) -> None:
        """Test-only: clear the registry."""
        with self._records_lock:
            self._records.clear()


__all__ = ["SubagentRecord", "SubagentRegistry"]
