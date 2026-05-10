"""HookHistory — ring-buffer of recent hook fires (in-process + on-disk).

Backs ``oc hooks list``'s last-fired column + ``oc hooks clear``.

Two layers:

1. **In-process deque** (``_history``) — fast read/write for the active
   process. Bounded at ``_HISTORY_MAXLEN`` per event so long-running
   daemons don't grow unbounded.
2. **On-disk JSONL** (``~/.opencomputer/<profile>/hook_history.jsonl``)
   — every ``record_fire`` also appends one line so a *fresh*
   ``oc hooks list`` invocation can read history written by previous
   processes (the agent loop, the gateway daemon, cron job runners).

Why both: the user's audit (2026-05-10) flagged "16 hook events, all
'Last fired: —'" because ``oc hooks list`` is a fresh process with no
record of what fired in the gateway / agent loop / chat session.
``_history`` was process-local; nothing persisted. This module now
writes through to disk so the diagnostic surface reflects reality.

Disk format (JSONL): one JSON object per line, keys ``event``,
``source_id``, ``ts_utc``, ``ok``, ``summary``. Append-only.

Truncation: when the file exceeds ``_DISK_MAX_BYTES`` (default 5 MiB)
the next ``record_fire`` rewrites it keeping only the most-recent
``_HISTORY_MAXLEN`` entries per event. Cheap because the file is JSONL
and parsing is line-by-line.

Thread-safety: ``record_fire`` uses an OS-level append (single
``write()`` of one line < page size) which is atomic on POSIX. The
in-process deque is GIL-protected.

Backwards compat: ``record_fire`` and ``iter_history`` keep their
signatures; existing call sites in agent/loop.py + gateway/* + cron/*
work unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("opencomputer.agent.hook_history")

_HISTORY_MAXLEN = 128
_SUMMARY_MAXLEN = 4096
_DISK_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB before compaction


@dataclass(frozen=True, slots=True)
class FireRecord:
    """One row of hook-fire history."""

    event: str
    source_id: str
    ts_utc: float
    ok: bool
    summary: str


_history: dict[str, deque[FireRecord]] = {}
_disk_lock = threading.Lock()
_disk_path_cache: Path | None = None
_disk_loaded = False


def _resolve_disk_path() -> Path | None:
    """Return ``~/.opencomputer/<profile>/hook_history.jsonl`` or None.

    Cached; per-process resolution. Returns None when profile resolution
    fails (e.g., running outside an OC environment in tests). Callers
    treat None as "skip disk persistence".
    """
    global _disk_path_cache
    if _disk_path_cache is not None:
        return _disk_path_cache
    try:
        from opencomputer.agent.config import _home

        profile_home = _home()
        path = profile_home / "hook_history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _disk_path_cache = path
        return path
    except Exception:  # noqa: BLE001 — disk persistence is best-effort
        return None


def _hydrate_from_disk() -> None:
    """One-time-per-process load of disk history into the in-process deques.

    Called lazily on first ``iter_history`` / ``all_events`` so a
    short-lived ``oc hooks list`` can see what longer-lived processes
    (agent loop, gateway daemon) have recorded.

    Tolerates malformed lines silently — the file is debug state, not
    audit state.
    """
    global _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True

    path = _resolve_disk_path()
    if path is None or not path.exists():
        return

    by_event: dict[str, deque[FireRecord]] = defaultdict(
        lambda: deque(maxlen=_HISTORY_MAXLEN)
    )
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rec = FireRecord(
                        event=str(obj.get("event", "")),
                        source_id=str(obj.get("source_id", "")),
                        ts_utc=float(obj.get("ts_utc", 0.0)),
                        ok=bool(obj.get("ok", False)),
                        summary=str(obj.get("summary", "")),
                    )
                except (ValueError, TypeError, KeyError):
                    continue  # skip malformed line
                if rec.event:
                    by_event[rec.event].append(rec)
    except OSError as exc:
        logger.debug("hook_history: read failed for %s: %s", path, exc)
        return

    # Merge into the in-process map. If a deque already exists (recorded
    # before hydration), prepend the disk records, preserving in-memory
    # ones at the tail (most-recent-wins for the deque cap).
    for event, recs in by_event.items():
        bucket = _history.get(event)
        if bucket is None:
            _history[event] = recs
        else:
            merged = deque(maxlen=_HISTORY_MAXLEN)
            for r in recs:
                merged.append(r)
            for r in bucket:
                merged.append(r)
            _history[event] = merged


def _maybe_compact_disk(path: Path) -> None:
    """If the disk file exceeds ``_DISK_MAX_BYTES``, rewrite keeping only
    the last ``_HISTORY_MAXLEN`` records per event.

    Called inside ``_disk_lock``.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= _DISK_MAX_BYTES:
        return

    by_event: dict[str, deque[FireRecord]] = defaultdict(
        lambda: deque(maxlen=_HISTORY_MAXLEN)
    )
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rec = FireRecord(
                        event=str(obj.get("event", "")),
                        source_id=str(obj.get("source_id", "")),
                        ts_utc=float(obj.get("ts_utc", 0.0)),
                        ok=bool(obj.get("ok", False)),
                        summary=str(obj.get("summary", "")),
                    )
                except (ValueError, TypeError, KeyError):
                    continue
                if rec.event:
                    by_event[rec.event].append(rec)

        tmp = path.with_suffix(path.suffix + ".compacting")
        with open(tmp, "w", encoding="utf-8") as fh:
            for recs in by_event.values():
                for r in recs:
                    fh.write(
                        json.dumps(
                            {
                                "event": r.event,
                                "source_id": r.source_id,
                                "ts_utc": r.ts_utc,
                                "ok": r.ok,
                                "summary": r.summary,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        os.replace(tmp, path)
        logger.info(
            "hook_history: compacted %s (was %d bytes, kept %d events)",
            path,
            size,
            len(by_event),
        )
    except OSError as exc:
        logger.debug("hook_history: compaction failed: %s", exc)


def _append_disk(rec: FireRecord) -> None:
    """Append one record to the disk JSONL. Best-effort; swallows OSError.

    Single-line ``write()`` is atomic on POSIX for buffers under
    PIPE_BUF (4 KiB) — our records cap at 4 KiB summary so the line
    typically lands well under 5 KiB. For very long summaries the write
    is interleavable but each line is still a complete JSON object on
    its own line; readers tolerate stray bytes per line.
    """
    path = _resolve_disk_path()
    if path is None:
        return
    line = (
        json.dumps(
            {
                "event": rec.event,
                "source_id": rec.source_id,
                "ts_utc": rec.ts_utc,
                "ok": rec.ok,
                "summary": rec.summary,
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    try:
        with _disk_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            _maybe_compact_disk(path)
    except OSError as exc:
        logger.debug("hook_history: append failed: %s", exc)


def record_fire(event: str, source_id: str, *, ok: bool, summary: str) -> None:
    """Append a fire record for ``event``. Non-blocking; swallow exceptions.

    Writes BOTH to the in-process deque AND to disk so a fresh
    ``oc hooks list`` invocation in another process can see this fire.
    """
    try:
        if len(summary) > _SUMMARY_MAXLEN:
            summary = summary[:_SUMMARY_MAXLEN] + "...[truncated]"
        rec = FireRecord(
            event=event,
            source_id=source_id,
            ts_utc=time.time(),
            ok=bool(ok),
            summary=summary,
        )
        bucket = _history.get(event)
        if bucket is None:
            bucket = deque(maxlen=_HISTORY_MAXLEN)
            _history[event] = bucket
        bucket.append(rec)
    except Exception:  # noqa: BLE001 — debug state must not crash the loop
        return

    # Disk persistence is best-effort; never propagates exceptions.
    _append_disk(rec)


def iter_history(event: str) -> Iterator[FireRecord]:
    """Iterate fire records for ``event`` (oldest → newest)."""
    _hydrate_from_disk()
    bucket = _history.get(event)
    if bucket is None:
        return iter(())
    return iter(list(bucket))


def all_events() -> list[str]:
    """List events that have any history."""
    _hydrate_from_disk()
    return sorted(_history.keys())


def clear_history() -> int:
    """Clear all history. Returns count of records cleared.

    Wipes both in-process AND on-disk state so ``oc hooks clear`` is
    not surprising the next time a fresh process reads the file.
    """
    _hydrate_from_disk()
    n = sum(len(b) for b in _history.values())
    _history.clear()

    path = _resolve_disk_path()
    if path is not None and path.exists():
        try:
            with _disk_lock:
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("hook_history: clear-on-disk failed: %s", exc)
    return n


def _reset_for_tests() -> None:
    """Test-only — wipe in-process state + disk-path cache.

    Production code never calls this. Tests set ``OPENCOMPUTER_HOME``
    to a tmp_path before constructing a fresh hook_history state.
    """
    global _disk_path_cache, _disk_loaded
    _history.clear()
    _disk_path_cache = None
    _disk_loaded = False


__all__ = [
    "FireRecord",
    "all_events",
    "clear_history",
    "iter_history",
    "record_fire",
]
