"""Cross-agent file state coordination — staleness warnings for sibling writes.

Complements :mod:`opencomputer.tools.delegation_coordinator` (which only
provides path locks). This module tracks **what each task has read and
written** so a write that would clobber a sibling subagent's changes can
be flagged before it lands.

Three failure modes this catches:

1. **Sibling write after my read.** Task A reads ``/proj/main.py``,
   sibling B writes ``/proj/main.py``, A then writes ``/proj/main.py``
   with content based on its (now stale) read — silently overwriting
   B's changes.
2. **External mtime drift.** A read ``/proj/main.py``, then a process
   outside the agent (linter, formatter, the user's editor) modified
   the file. A's next write would similarly clobber.
3. **Write-without-read.** A writes ``/proj/main.py`` but never read
   it — likely a half-informed edit.

Each failure mode returns a human-readable warning string from
:func:`check_stale`; callers (Write / Edit / MultiEdit tools) decide
whether to refuse or merely surface the warning to the model.

Task identity comes from
:func:`opencomputer.observability.logging_config._session_id_var`. Each
subagent's :meth:`SessionDB.create_session` binds a fresh session id to
that ContextVar; ContextVar copy-on-task-spawn semantics mean concurrent
sibling subagents see different ids without manual plumbing.

Disabled by ``OPENCOMPUTER_DISABLE_FILE_STATE_GUARD=1`` for tests /
debugging.

Ported from Hermes ``tools/file_state.py`` (Apache-2.0). Adaptations:

- Task-id source: observability ContextVar (vs. Hermes's manual ``task_id``
  thread-through).
- Env var renamed to ``OPENCOMPUTER_DISABLE_FILE_STATE_GUARD``.
- Thread-locking model unchanged — file I/O in OC's tools runs on a
  thread pool, so ``threading.Lock`` is the right primitive.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

# (mtime_at_read, read_ts, partial_view).
# ``partial=True`` when the read used offset/limit pagination — we still
# warn on next write because the agent has only seen a windowed view.
ReadStamp = tuple[float, float, bool]

# Memory caps so a long session can't accumulate unbounded path state.
_MAX_PATHS_PER_AGENT = 4096
_MAX_GLOBAL_WRITERS = 4096

# Sentinel used when no session id is bound. Behaves like its own
# unique task id so the ``writes_since`` reminder still fires for
# top-level (non-delegated) work.
_NO_TASK_ID = "<no-task>"


class FileStateRegistry:
    """Process-wide coordinator for cross-agent file edits.

    Two state dicts:

    - ``_reads[task_id][path] -> ReadStamp`` — what each task has read.
    - ``_last_writer[path] -> (task_id, ts)`` — globally last writer.

    Plus per-path locks so a Read→Edit→Write critical section can be
    serialized without serializing the whole tool registry.
    """

    def __init__(self) -> None:
        self._reads: dict[str, dict[str, ReadStamp]] = defaultdict(dict)
        self._last_writer: dict[str, tuple[str, float]] = {}
        self._path_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()  # guards _path_locks
        self._state_lock = threading.Lock()  # guards _reads + _last_writer

    # ─── Path-lock plumbing ─────────────────────────────────────────

    def _lock_for(self, resolved: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._path_locks.get(resolved)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[resolved] = lock
            return lock

    @contextmanager
    def lock_path(self, resolved: str):
        """Per-path lock for read→modify→write critical sections.

        Threads on the same path serialize; different paths run in
        parallel. Cheap when uncontended.
        """
        lock = self._lock_for(resolved)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    # ─── Read/write accounting ──────────────────────────────────────

    def record_read(
        self,
        task_id: str,
        resolved: str,
        *,
        partial: bool = False,
        mtime: float | None = None,
    ) -> None:
        if _disabled():
            return
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                # File doesn't exist — nothing to record. The write
                # path will treat this as "first touch" anyway.
                return
        now = time.time()
        with self._state_lock:
            agent_reads = self._reads[task_id]
            agent_reads[resolved] = (float(mtime), now, bool(partial))
            _cap_dict(agent_reads, _MAX_PATHS_PER_AGENT)

    def note_write(
        self,
        task_id: str,
        resolved: str,
        *,
        mtime: float | None = None,
    ) -> None:
        """Record a successful write.

        Two effects:
        1. Globally: this task is now the last writer of the path.
        2. Locally: a write is an implicit read — the writer's view
           is now consistent with disk, so subsequent writes by the
           same task don't warn about staleness.
        """
        if _disabled():
            return
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        now = time.time()
        with self._state_lock:
            self._last_writer[resolved] = (task_id, now)
            _cap_dict(self._last_writer, _MAX_GLOBAL_WRITERS)
            self._reads[task_id][resolved] = (float(mtime), now, False)
            _cap_dict(self._reads[task_id], _MAX_PATHS_PER_AGENT)

    def check_stale(self, task_id: str, resolved: str) -> str | None:
        """Return a warning if writing this path would clobber recent state.

        Three classes of staleness, severity-ordered:

        1. Sibling subagent wrote after this task's last read.
        2. mtime on disk doesn't match my last-recorded mtime
           (external editor, formatter, ...).
        3. This task never read the file (write-without-read).

        Returns ``None`` when the write is safe.
        """
        if _disabled():
            return None
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
            last_writer = self._last_writer.get(resolved)

        # Brand-new write to a brand-new file: both stamp and last_writer
        # are None. Let the file tools' usual existence-check handle this
        # — there's no staleness to talk about.
        if stamp is None and last_writer is None:
            return None

        try:
            current_mtime = os.path.getmtime(resolved)
        except OSError:
            # File doesn't exist — write will create it; not stale.
            return None

        # Case 1: another task wrote after our last read.
        if last_writer is not None:
            writer_tid, writer_ts = last_writer
            if writer_tid != task_id:
                if stamp is None:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} but this agent never read it. "
                        "Read the file before writing to avoid overwriting "
                        "the sibling's changes."
                    )
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} at {_fmt_ts(writer_ts)} — after "
                        f"this agent's last read at {_fmt_ts(read_ts)}. "
                        "Re-read the file before writing."
                    )

        # Case 2: external / unknown modification (mtime drifted).
        if stamp is not None:
            read_mtime, _read_ts, partial = stamp
            if current_mtime != read_mtime:
                return (
                    f"{resolved} was modified since you last read it on "
                    "disk (external edit or unrecorded writer). "
                    "Re-read the file before writing."
                )
            if partial:
                return (
                    f"{resolved} was last read with offset/limit pagination "
                    "(partial view). Re-read the whole file before "
                    "overwriting it."
                )

        # Case 3: agent truly never read this file (but a sibling did
        # write to it earlier — ``last_writer`` set, ``stamp`` None).
        if stamp is None:
            return (
                f"{resolved} was not read by this agent. "
                "Read the file first so you can write an informed edit."
            )

        return None

    # ─── Reminder helper for delegate.py ───────────────────────────

    def writes_since(
        self,
        exclude_task_id: str,
        since_ts: float,
        paths: Iterable[str],
    ) -> dict[str, list[str]]:
        """Return ``{writer_task_id: [paths]}`` for writes done after
        ``since_ts`` by tasks OTHER than ``exclude_task_id``.

        Used by ``DelegateTool`` to append a "subagent X modified files
        you previously read" reminder onto the delegation result.
        """
        if _disabled():
            return {}
        paths_set = set(paths)
        out: dict[str, list[str]] = defaultdict(list)
        with self._state_lock:
            for p, (writer_tid, ts) in self._last_writer.items():
                if writer_tid == exclude_task_id:
                    continue
                if ts < since_ts:
                    continue
                if p in paths_set:
                    out[writer_tid].append(p)
        return dict(out)

    def known_reads(self, task_id: str) -> list[str]:
        """Resolved paths this task has read (for delegate reminders)."""
        if _disabled():
            return []
        with self._state_lock:
            return list(self._reads.get(task_id, {}).keys())

    def clear(self) -> None:
        """Reset all state. Tests only."""
        with self._state_lock:
            self._reads.clear()
            self._last_writer.clear()
        with self._meta_lock:
            self._path_locks.clear()


# ─── Module-level singleton + helpers ────────────────────────────────

_registry = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _registry


def _disabled() -> bool:
    # Re-read each call so monkeypatch.setenv toggles are honoured.
    return os.environ.get("OPENCOMPUTER_DISABLE_FILE_STATE_GUARD", "").strip() == "1"


def current_task_id() -> str:
    """Return the task-id for the currently-running tool call.

    Pulls from the observability ContextVar (set by SessionDB.create_session).
    Falls back to a sentinel when no session is bound (CLI bootstrap,
    test fixtures that don't open a real session).
    """
    try:
        from opencomputer.observability.logging_config import _session_id_var
    except ImportError:
        return _NO_TASK_ID
    return _session_id_var.get() or _NO_TASK_ID


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _cap_dict(d: dict, limit: int) -> None:
    """Drop oldest insertion-order entries until len(d) <= limit."""
    over = len(d) - limit
    if over <= 0:
        return
    it = iter(d)
    for _ in range(over):
        try:
            d.pop(next(it))
        except (StopIteration, KeyError):
            break


# ─── Convenience wrappers (short names at call sites) ────────────────


def record_read(
    path: str | Path, *, task_id: str | None = None, partial: bool = False
) -> None:
    tid = task_id or current_task_id()
    _registry.record_read(tid, _resolve(path), partial=partial)


def note_write(path: str | Path, *, task_id: str | None = None) -> None:
    tid = task_id or current_task_id()
    _registry.note_write(tid, _resolve(path))


def check_stale(path: str | Path, *, task_id: str | None = None) -> str | None:
    tid = task_id or current_task_id()
    return _registry.check_stale(tid, _resolve(path))


def lock_path(path: str | Path):
    return _registry.lock_path(_resolve(path))


def writes_since(
    exclude_task_id: str,
    since_ts: float,
    paths: Iterable[str | Path],
) -> dict[str, list[str]]:
    return _registry.writes_since(
        exclude_task_id, since_ts, [_resolve(p) for p in paths]
    )


def known_reads(task_id: str | None = None) -> list[str]:
    return _registry.known_reads(task_id or current_task_id())


def _resolve(path: str | Path) -> str:
    """Best-effort canonicalization. Prefers absolute path even if the
    file doesn't exist (so first-write paths still produce stable keys).
    """
    p = Path(path)
    try:
        return str(p.resolve(strict=False))
    except OSError:
        return str(p.absolute())


__all__ = [
    "FileStateRegistry",
    "check_stale",
    "current_task_id",
    "get_registry",
    "known_reads",
    "lock_path",
    "note_write",
    "record_read",
    "writes_since",
]
