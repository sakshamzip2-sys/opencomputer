"""SubagentStore — sqlite write-through for SubagentRegistry.

The :class:`opencomputer.agent.subagent_registry.SubagentRegistry` is an
in-process tracker of running and recently-finished subagents. Without a
backing store, ``oc agents history`` shows nothing across process
restarts, and cross-process forensics (Hermes parity) is impossible.

This module supplies the persistent companion to that registry:

* :class:`StoredSubagent` — the on-disk shape (no live ``cancel_event`` /
  ``event_loop``, since those can't be serialised).
* :class:`SubagentStore` — small sqlite IO facade. Owns short-lived
  connections (open + write + close) so it never holds a transaction
  open on the hot path. Shares the same ``sessions.db`` file used by
  :class:`opencomputer.agent.state.SessionDB`; WAL mode allows
  concurrent readers/writers without contention.
* PID liveness tuple (``host_pid``, ``host_started_at``) — a
  ``running`` record whose pid is no longer alive (or whose pid was
  reused — start-time mismatch) is reported as ``orphaned`` at read
  time. This is the cheap half of crash detection; a periodic
  heartbeat is deferred (see design doc 2026-05-10).

Schema is defined by migration v15→v16 in
:mod:`opencomputer.agent.state`. The store assumes the migration has
run; constructing a store on a path whose DB is older raises
:class:`SubagentStoreUnavailable` rather than corrupting state.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

_log = logging.getLogger(__name__)

#: Captured at import time so every record written by this process can be
#: matched against ``os.getpid()`` later for orphan detection. We use
#: process start-time (not just pid) because pids get reused after
#: termination — a record whose pid is alive but whose start-time
#: doesn't match is from a *different* process that happens to share the
#: pid number.
_PROCESS_PID: int = os.getpid()


def _process_started_at() -> float:
    """Best-effort process start-time in epoch seconds.

    Uses ``psutil`` when available (cross-platform), falling back to the
    current wall-clock when not. The fallback means ``is_orphaned``
    becomes "is this pid alive?" only — start-time matching degrades to
    a no-op. That's still a strict superset of "RAM-only" (the prior
    state) so the degradation is acceptable.
    """
    try:
        import psutil  # type: ignore[import-not-found]

        return float(psutil.Process(_PROCESS_PID).create_time())
    except Exception:  # noqa: BLE001 — fallback to wall-clock
        return time.time()


_PROCESS_STARTED_AT: float = _process_started_at()


def _pid_alive(pid: int, started_at: float) -> bool:
    """True iff the process with ``pid`` is alive AND was started within
    ±2 seconds of ``started_at``.

    The 2-second window covers psutil's create_time precision (typically
    millisecond) and the wall-clock-fallback drift between process spawn
    and ``_process_started_at()`` resolution. A wider window risks
    false-positives on pid reuse; a narrower one risks false-negatives
    on slower CI hosts.

    Returns True for our own pid without checking — we know we're alive.
    """
    if pid == _PROCESS_PID:
        return True
    try:
        import psutil  # type: ignore[import-not-found]

        proc = psutil.Process(pid)
        return abs(proc.create_time() - started_at) < 2.0
    except Exception:  # noqa: BLE001
        # psutil unavailable or process gone → fall back to kill(0).
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        # kill(0) succeeded — pid exists but we can't verify start-time
        # without psutil. Trust the caller.
        return True


class SubagentStoreUnavailable(RuntimeError):  # noqa: N818 — matches WorktreeNotAvailable / IsolationFailed in this package
    """Raised when constructing a SubagentStore against a DB older than
    schema v16, or against a path that doesn't exist.

    Callers catch this to fall back to in-memory-only behavior so the
    legacy code path remains usable.
    """


@dataclass(frozen=True, slots=True)
class StoredSubagent:
    """One row from the ``subagents`` table.

    Mirrors :class:`opencomputer.agent.subagent_registry.SubagentRecord`
    minus the live-state fields (``cancel_event``, ``event_loop``).
    Always carries ``host_pid`` + ``host_started_at`` for
    :meth:`is_orphaned`.
    """

    agent_id: str
    parent_session_id: str | None
    child_session_id: str | None
    parent_agent_id: str | None
    goal: str
    started_at: datetime
    ended_at: datetime | None
    state: str
    error: str | None
    role: str
    agent_template: str | None
    isolation_mode: str
    depth: int
    host_pid: int
    host_started_at: float

    @property
    def is_orphaned(self) -> bool:
        """A ``running`` record whose host process is no longer alive
        (or whose pid was reused — start-time mismatch).

        Always returns ``False`` for non-running states (``completed``,
        ``failed``, ``killed``) — those are terminal, the question
        "did the parent crash" doesn't apply.
        """
        if self.state != "running" or self.ended_at is not None:
            return False
        return not _pid_alive(self.host_pid, self.host_started_at)

    @property
    def display_state(self) -> str:
        """``state`` augmented with ``orphaned`` when applicable.

        Prefer this in user-facing rendering (e.g. ``oc agents history``)
        — orphaned records read as "running" otherwise, which is wrong.
        """
        return "orphaned" if self.is_orphaned else self.state


class SubagentStore:
    """Sqlite write-through for the subagents table.

    Constructed lazily — pass a path to a sessions.db that's already at
    schema v16 or higher. Each method opens a fresh connection (timeout
    5s, WAL journal) so concurrent registries on different processes
    don't block each other indefinitely.

    Thread-safe: the instance lock serialises writes from concurrent
    threads in the same process. Cross-process serialisation is provided
    by sqlite's WAL + ``BEGIN IMMEDIATE`` semantics. WAL means readers
    never block writers and writers never block readers — only writer-
    writer pairs serialise.
    """

    #: Default connect timeout — covers most contention windows without
    #: turning into a wedge. Sqlite's busy-handler retries internally.
    _DEFAULT_TIMEOUT_S: ClassVar[float] = 5.0

    def __init__(self, db_path: Path | str, *, allow_create: bool = False) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        if not self._db_path.exists() and not allow_create:
            raise SubagentStoreUnavailable(
                f"sessions.db not found at {self._db_path}; "
                "construct SessionDB first or pass allow_create=True"
            )
        # Validate schema compatibility immediately so callers get a
        # clear error at construction time, not at first write.
        self._validate_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _validate_schema(self) -> None:
        """Confirm the ``subagents`` table is present.

        Sqlite quirk: a missing ``subagents`` table raises
        ``OperationalError`` only on first reference, which on an
        otherwise-healthy DB would be the first ``upsert`` call. Probe
        it here so the failure surfaces at construction.
        """
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='subagents'"
                ).fetchone()
        except sqlite3.OperationalError as exc:
            raise SubagentStoreUnavailable(
                f"cannot open {self._db_path}: {exc}"
            ) from exc
        if row is None:
            raise SubagentStoreUnavailable(
                f"{self._db_path} is older than schema v16 (subagents table "
                "not found); upgrade by re-opening SessionDB or running "
                "migrations explicitly"
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Short-lived connection with WAL + standard pragmas.

        Yields a connection that auto-commits on context exit (or rolls
        back on exception). Always closes the connection — sqlite leaks
        file handles otherwise.
        """
        with self._lock:
            c = sqlite3.connect(
                str(self._db_path),
                timeout=self._DEFAULT_TIMEOUT_S,
                isolation_level=None,  # autocommit; we manage txn explicitly
            )
            c.row_factory = sqlite3.Row
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA foreign_keys=ON")
                yield c
            finally:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass

    # ─── writes ───────────────────────────────────────────────────

    def upsert(
        self,
        *,
        agent_id: str,
        parent_session_id: str | None,
        child_session_id: str | None,
        parent_agent_id: str | None,
        goal: str,
        started_at: datetime,
        state: str = "running",
        role: str = "leaf",
        agent_template: str | None = None,
        isolation_mode: str = "none",
        depth: int = 0,
    ) -> None:
        """Insert-or-replace a subagent row.

        The ``host_pid`` + ``host_started_at`` are sourced from the
        current process — a record is always claimed by the writing
        process. A subsequent process that re-registers the same
        ``agent_id`` (e.g. a long-running test fixture using the
        ``reset()`` test helper across processes) would overwrite both
        — that's correct: whoever registers last owns liveness.

        All inputs are passed as parameters (no string concatenation);
        sqlite handles the escaping. ``goal`` is truncated to 200 chars
        to mirror the in-memory record's behavior.
        """
        if not agent_id:
            raise ValueError("agent_id must be non-empty")
        if not goal:
            raise ValueError("goal must be non-empty")
        if state not in {"running", "completed", "failed", "killed"}:
            raise ValueError(
                f"invalid state {state!r} (expected running|completed|failed|killed)"
            )
        if role not in {"leaf", "orchestrator"}:
            raise ValueError(
                f"invalid role {role!r} (expected leaf|orchestrator)"
            )
        if isolation_mode not in {"none", "worktree", "copy"}:
            raise ValueError(
                f"invalid isolation_mode {isolation_mode!r} "
                f"(expected none|worktree|copy)"
            )
        if depth < 0:
            raise ValueError(f"depth must be >= 0 (got {depth})")
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO subagents (
                    agent_id, parent_session_id, child_session_id,
                    parent_agent_id, goal, started_at, ended_at, state,
                    error, role, agent_template, isolation_mode, depth,
                    host_pid, host_started_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    parent_session_id or None,
                    child_session_id or None,
                    parent_agent_id or None,
                    goal[:200],
                    started_at.timestamp(),
                    state,
                    role,
                    agent_template,
                    isolation_mode,
                    int(depth),
                    _PROCESS_PID,
                    _PROCESS_STARTED_AT,
                ),
            )

    #: Field names whose UPDATE this store accepts. Anything else raises
    #: KeyError so misconfigured callers don't silently no-op (which was
    #: the original RAM-only registry's behavior — we make persistence
    #: stricter).
    _UPDATABLE_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "child_session_id",
        "ended_at",
        "state",
        "error",
        "current_tool",  # accepted-but-not-persisted (RAM-only)
        "tokens_used",   # accepted-but-not-persisted (RAM-only)
    })

    #: Fields actually persisted by ``update``. The two RAM-only fields
    #: in :data:`_UPDATABLE_FIELDS` get filtered out at write time so
    #: the SQL statement is well-formed.
    _PERSISTED_UPDATE_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "child_session_id",
        "ended_at",
        "state",
        "error",
    })

    def update(self, agent_id: str, **fields: Any) -> None:
        """Update fields on an existing record. Silently no-ops on
        unknown agent_id (matches the registry's update semantics).

        ``ended_at`` may be a ``datetime`` (converted to epoch seconds)
        or a float. ``state`` is validated against the same set used by
        :meth:`upsert`. Unknown fields raise ``KeyError``.
        """
        if not agent_id:
            raise ValueError("agent_id must be non-empty")
        unknown = set(fields) - self._UPDATABLE_FIELDS
        if unknown:
            raise KeyError(
                f"unknown subagent field(s): {sorted(unknown)}"
            )
        # Filter out RAM-only fields: they're accepted by the registry's
        # update() but don't have a sqlite column.
        persistable = {
            k: v for k, v in fields.items() if k in self._PERSISTED_UPDATE_FIELDS
        }
        if not persistable:
            return
        if "state" in persistable and persistable["state"] not in {
            "running", "completed", "failed", "killed"
        }:
            raise ValueError(f"invalid state {persistable['state']!r}")
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in persistable.items():
            if k == "ended_at":
                if isinstance(v, datetime):
                    v = v.timestamp()
                elif v is not None and not isinstance(v, (int, float)):
                    raise ValueError(
                        f"ended_at must be datetime|float|None, got {type(v).__name__}"
                    )
            elif k == "error" and v is not None and not isinstance(v, str):
                v = str(v)[:200]
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(agent_id)
        with self._conn() as c:
            c.execute(
                f"UPDATE subagents SET {', '.join(sets)} WHERE agent_id = ?",
                vals,
            )

    # ─── reads ────────────────────────────────────────────────────

    def history(self, *, limit: int = 50) -> list[StoredSubagent]:
        """Return the last ``limit`` non-running records, newest first.

        Newest = highest ``ended_at``. Running records are excluded —
        they show up in :meth:`list_running` instead. Orphaned-running
        records (host_pid dead) are NOT auto-promoted to history; the
        caller decides whether to render them as orphaned via
        :attr:`StoredSubagent.display_state`.
        """
        if limit <= 0:
            return []
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT * FROM subagents
                WHERE state != 'running'
                ORDER BY ended_at DESC NULLS LAST
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_stored(r) for r in rows]

    def list_running(self, *, include_orphans: bool = True) -> list[StoredSubagent]:
        """Return every record whose stored state is ``running``.

        ``include_orphans=False`` filters out records whose host process
        is no longer alive (useful for "what's truly active right now"
        queries). Default ``True`` matches the prior in-memory behavior
        — orphans appear with ``display_state='orphaned'``.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM subagents WHERE state = 'running' "
                "ORDER BY started_at"
            ).fetchall()
        records = [self._row_to_stored(r) for r in rows]
        if include_orphans:
            return records
        return [r for r in records if not r.is_orphaned]

    def find_by_parent(self, parent_session_id: str) -> list[StoredSubagent]:
        """Children of one delegating session, ordered by start time.

        Empty list when no children — the only failure case.
        """
        if not parent_session_id:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM subagents WHERE parent_session_id = ? "
                "ORDER BY started_at",
                (parent_session_id,),
            ).fetchall()
        return [self._row_to_stored(r) for r in rows]

    def find_by_child(self, child_session_id: str) -> StoredSubagent | None:
        """The subagents row whose child_session_id matches, or None."""
        if not child_session_id:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM subagents WHERE child_session_id = ? LIMIT 1",
                (child_session_id,),
            ).fetchone()
        return self._row_to_stored(row) if row is not None else None

    def get(self, agent_id: str) -> StoredSubagent | None:
        if not agent_id:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM subagents WHERE agent_id = ? LIMIT 1",
                (agent_id,),
            ).fetchone()
        return self._row_to_stored(row) if row is not None else None

    def reset(self) -> None:
        """Test-only: empty the subagents table.

        Production code should never call this — registry restarts are
        normal, but data wiping is destructive. Tests use this to
        ensure cross-test isolation.
        """
        with self._conn() as c:
            c.execute("DELETE FROM subagents")

    @staticmethod
    def _row_to_stored(r: sqlite3.Row) -> StoredSubagent:
        ended = r["ended_at"]
        return StoredSubagent(
            agent_id=r["agent_id"],
            parent_session_id=r["parent_session_id"],
            child_session_id=r["child_session_id"],
            parent_agent_id=r["parent_agent_id"],
            goal=r["goal"],
            started_at=datetime.fromtimestamp(r["started_at"], tz=UTC),
            ended_at=(
                datetime.fromtimestamp(ended, tz=UTC) if ended is not None else None
            ),
            state=r["state"],
            error=r["error"],
            role=r["role"],
            agent_template=r["agent_template"],
            isolation_mode=r["isolation_mode"],
            depth=int(r["depth"]),
            host_pid=int(r["host_pid"]),
            host_started_at=float(r["host_started_at"]),
        )


__all__ = [
    "StoredSubagent",
    "SubagentStore",
    "SubagentStoreUnavailable",
]
