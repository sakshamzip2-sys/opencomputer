"""SubagentRegistry — in-process tracker for active subagents.

Backs ``oc agents list/kill/history``. Best-effort: cross-process
subagents (none in the current architecture) wouldn't appear here.

Hermes parity (2026-05-08): closes the operational gap of "what's
running right now and how do I cancel it" without requiring a full
TUI overlay (deferred).

delegate-lineage (2026-05-10): an optional :class:`SubagentStore` can
be attached so register / update / kill write through to sqlite.
History queries then survive process restart and become visible to
``oc sessions tree``. The in-memory dict remains authoritative for
live-state fields (``cancel_event``, ``event_loop``) which can't be
serialised. Tests opt out of the store with the autouse fixture in
``tests/agent/test_subagent_registry.py`` — back-compat preserved.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class SubagentRecord:
    """One row in the registry. A subagent's lifecycle.

    ``state`` transitions: running → completed | failed | killed.
    ``cancel_event`` and ``event_loop`` are set on register; the kill()
    path uses ``event_loop.call_soon_threadsafe(cancel_event.set)`` so
    cancellation crosses the loop boundary safely (F4 audit fix from
    the 2026-05-08 plan-audit).

    delegate-lineage (2026-05-10) adds:

    - ``parent_session_id``: parent loop's ``_current_session_id`` at
      the moment delegation was issued. Empty string = no parent
      session (CLI bootstrap path).
    - ``child_session_id``: assigned once the child loop has created
      its session row; populated post-run from the child loop's
      ``ConversationResult.session_id``.
    - ``role`` / ``agent_template`` / ``isolation_mode`` / ``depth``:
      delegation-shape metadata mirrored to sqlite for ``oc agents
      history`` and tree walks.
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
    # delegate-lineage (2026-05-10) — persistent fields:
    parent_session_id: str = ""
    child_session_id: str = ""
    role: str = "leaf"
    agent_template: str | None = None
    isolation_mode: str = "none"
    depth: int = 0


class SubagentRegistry:
    """Thread-safe singleton tracking subagent lifecycles."""

    _instance: SubagentRegistry | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._records: dict[str, SubagentRecord] = {}
        self._records_lock = threading.RLock()
        # delegate-lineage (2026-05-10): optional sqlite write-through.
        # ``None`` means RAM-only mode (legacy + test fixture default).
        self._store: Any = None  # SubagentStore | None — typed Any to avoid import cycle

    @classmethod
    def instance(cls) -> SubagentRegistry:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def attach_store(self, store: Any) -> None:
        """Attach a :class:`SubagentStore` for sqlite write-through.

        Idempotent: re-attaching the same store is a no-op; attaching a
        different store replaces the prior one (the new store inherits
        any in-memory records that haven't yet been persisted).
        """
        with self._records_lock:
            if self._store is store:
                return
            self._store = store
            records_to_backfill = list(self._records.values())
        # Backfill: write the in-memory records so a fresh store
        # gets a complete picture. Best-effort — a sqlite IO error
        # leaves the records in memory and logs a warning.
        for rec in records_to_backfill:
            try:
                store.upsert(
                    agent_id=rec.agent_id,
                    parent_session_id=rec.parent_session_id,
                    child_session_id=rec.child_session_id or None,
                    parent_agent_id=rec.parent_id,
                    goal=rec.goal,
                    started_at=rec.started_at,
                    state=rec.state,
                    role=rec.role,
                    agent_template=rec.agent_template,
                    isolation_mode=rec.isolation_mode,
                    depth=rec.depth,
                )
                if rec.ended_at is not None or rec.error is not None or rec.state != "running":
                    store.update(
                        rec.agent_id,
                        ended_at=rec.ended_at,
                        state=rec.state,
                        error=rec.error,
                    )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "SubagentStore backfill failed for %s: %s",
                    rec.agent_id,
                    exc,
                )

    def detach_store(self) -> None:
        """Drop the sqlite write-through. Subsequent register/update/kill
        run RAM-only. Used by test fixtures and shutdown paths.
        """
        with self._records_lock:
            self._store = None

    def has_store(self) -> bool:
        with self._records_lock:
            return self._store is not None

    def register(
        self,
        *,
        parent_id: str | None,
        goal: str,
        event_loop: asyncio.AbstractEventLoop | None = None,
        parent_session_id: str = "",
        role: str = "leaf",
        agent_template: str | None = None,
        isolation_mode: str = "none",
        depth: int = 0,
    ) -> SubagentRecord:
        """Add a running subagent record. Returns the record (for
        ``update`` / ``kill`` callbacks).

        ``event_loop`` defaults to the running loop at the time of call —
        captures the child's loop so cross-thread kill via
        ``call_soon_threadsafe`` works.

        The new keyword args (``parent_session_id``, ``role``,
        ``agent_template``, ``isolation_mode``, ``depth``) are
        delegate-lineage additions. All have defaults so legacy callers
        (Hermes-parity tests, in particular) keep working unchanged.
        """
        if event_loop is None:
            try:
                event_loop = asyncio.get_running_loop()
            except RuntimeError:
                event_loop = None
        if role not in {"leaf", "orchestrator"}:
            raise ValueError(f"role must be leaf|orchestrator (got {role!r})")
        if isolation_mode not in {"none", "worktree", "copy"}:
            raise ValueError(
                f"isolation_mode must be none|worktree|copy (got {isolation_mode!r})"
            )
        if depth < 0:
            raise ValueError(f"depth must be >= 0 (got {depth})")
        rec = SubagentRecord(
            agent_id=f"sub-{uuid.uuid4().hex[:10]}",
            parent_id=parent_id,
            goal=goal[:200],
            started_at=datetime.now(UTC),
            cancel_event=asyncio.Event() if event_loop is not None else None,
            event_loop=event_loop,
            parent_session_id=parent_session_id or "",
            role=role,
            agent_template=agent_template,
            isolation_mode=isolation_mode,
            depth=int(depth),
        )
        with self._records_lock:
            self._records[rec.agent_id] = rec
            store = self._store
        if store is not None:
            try:
                store.upsert(
                    agent_id=rec.agent_id,
                    parent_session_id=rec.parent_session_id,
                    child_session_id=rec.child_session_id or None,
                    parent_agent_id=rec.parent_id,
                    goal=rec.goal,
                    started_at=rec.started_at,
                    state=rec.state,
                    role=rec.role,
                    agent_template=rec.agent_template,
                    isolation_mode=rec.isolation_mode,
                    depth=rec.depth,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "SubagentStore upsert failed for %s: %s — record stays in-memory",
                    rec.agent_id,
                    exc,
                )
        return rec

    def update(self, agent_id: str, **fields: Any) -> None:
        """Mutate a record's fields. Silently no-ops on unknown id.

        Persisted fields (``state``, ``ended_at``, ``error``,
        ``child_session_id``) write through to the attached store.
        Non-persistent fields (``current_tool``, ``tokens_used``) stay
        in memory only.
        """
        with self._records_lock:
            rec = self._records.get(agent_id)
            if rec is None:
                return
            for k, v in fields.items():
                if hasattr(rec, k):
                    setattr(rec, k, v)
            store = self._store
        if store is not None and fields:
            try:
                store.update(agent_id, **fields)
            except KeyError:
                # Unknown sqlite field — silently ignore (the in-memory
                # update already accepted the field via hasattr; the
                # store is stricter on purpose, but we don't break the
                # caller's flow).
                pass
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "SubagentStore update failed for %s: %s",
                    agent_id,
                    exc,
                )

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
            store = self._store

        if store is not None:
            try:
                store.update(
                    agent_id,
                    state="killed",
                    ended_at=rec.ended_at,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "SubagentStore update on kill failed for %s: %s",
                    agent_id,
                    exc,
                )

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
        """Most recent N completed/failed/killed records (newest first).

        With a store attached, merges in-memory records with sqlite
        records (deduped on ``agent_id``, in-memory wins because it
        carries live-state fields). Without a store, returns only the
        in-memory dict — the prior behavior.
        """
        with self._records_lock:
            in_mem_ended = [
                r for r in self._records.values() if r.state != "running"
            ]
            store = self._store

        store_promoted: list[SubagentRecord] = []
        if store is not None:
            try:
                stored_rows = store.history(limit=limit * 2)  # over-fetch for merge
            except Exception as exc:  # noqa: BLE001
                _log.warning("SubagentStore history failed: %s", exc)
                stored_rows = []
            in_mem_ids = {r.agent_id for r in in_mem_ended}
            for row in stored_rows:
                if row.agent_id in in_mem_ids:
                    continue
                # Promote stored row to a SubagentRecord so the public
                # API stays a single type. Live-state fields are None.
                store_promoted.append(
                    SubagentRecord(
                        agent_id=row.agent_id,
                        parent_id=row.parent_agent_id,
                        goal=row.goal,
                        started_at=row.started_at,
                        ended_at=row.ended_at,
                        state=row.state,
                        error=row.error,
                        cancel_event=None,
                        event_loop=None,
                        parent_session_id=row.parent_session_id or "",
                        child_session_id=row.child_session_id or "",
                        role=row.role,
                        agent_template=row.agent_template,
                        isolation_mode=row.isolation_mode,
                        depth=row.depth,
                    )
                )
        merged = in_mem_ended + store_promoted
        epoch = datetime.fromtimestamp(0, tz=UTC)
        merged.sort(key=lambda r: r.ended_at or epoch, reverse=True)
        return merged[:limit]

    def reset(self) -> None:
        """Test-only: clear the registry.

        Resets the in-memory dict AND the attached store (if any). After
        ``reset()`` the registry is back to empty regardless of mode —
        important for cross-test isolation.
        """
        with self._records_lock:
            self._records.clear()
            store = self._store
        if store is not None:
            try:
                store.reset()
            except Exception as exc:  # noqa: BLE001
                _log.warning("SubagentStore reset failed: %s", exc)


__all__ = ["SubagentRecord", "SubagentRegistry"]
