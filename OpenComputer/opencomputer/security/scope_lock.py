"""Machine-local scoped locks (hermes parity).

Ported from ``sources/hermes-agent-2026.4.23/gateway/status.py:464`` —
prevents two local processes from holding the same external identity at
the same time. The original use case is the one we hit in the
v2026.4.26 Telegram E2E: Claude Code's Telegram channel adapter (PID
45409) was already polling ``@Terraform_368Bot``, OC's gateway tried
the same bot, Telegram serves long-poll updates to whoever asked
first, and OC silently saw zero traffic.

A scoped lock at startup turns that into a clear refusal:

    Telegram bot token already in use by PID 45409.
    Stop that process or run with a different bot token.

vs. the previous "polling ... but nothing arrives, why?" debugging
session.

Storage layout: ``<lock_dir>/<scope>-<sha256(identity)[:16]>.lock``
holding a JSON record with ``pid`` + ``start_time`` for staleness
detection. Default lock_dir is ``~/.opencomputer/locks``; override
with the ``OPENCOMPUTER_LOCK_DIR`` env var (used by tests).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _scope_hash(identity: str) -> str:
    """Truncated sha256 of the identity. 16 hex chars = 64 bits — plenty for
    collision avoidance among locks on a single machine, while keeping the
    lock file name readable + the secret token unrecoverable from disk."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _get_lock_dir() -> Path:
    override = os.environ.get("OPENCOMPUTER_LOCK_DIR")
    if override:
        return Path(override)
    from opencomputer.agent.config import _home

    return _home() / "locks"


def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


def _get_process_start_time(pid: int) -> int | None:
    """Read a process's kernel start time on Linux (clock ticks since boot).

    Used for stale-lock detection — a holder PID that has the same number
    but a different start_time is a different process that recycled the
    PID, so the lock is stale. ``/proc/<pid>/stat`` field 22 is the start
    time (man 5 proc). Returns ``None`` on macOS / other platforms where
    /proc is unavailable; callers fall back to the cheaper "is the PID
    still alive at all?" check via ``os.kill(pid, 0)``.
    """
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        return int(stat_path.read_text().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def _build_pid_record() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
    }


def _read_lock_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _is_lock_stale(existing: dict[str, Any]) -> bool:
    """Decide whether a lock file points at a no-longer-running owner.

    Three layers:

    1. PID can't be parsed → stale (corrupt lock).
    2. PID is dead → stale (process exited without releasing).
    3. PID is alive but start_time differs from what's on disk → stale
       (PID was recycled; same number, different process).

    Layer 3 only fires on Linux (where ``/proc/<pid>/stat`` exists). On
    macOS we degrade to layer 2 only — the false-positive risk is "PID
    recycled within seconds, lock incorrectly considered live", which is
    extremely rare and at worst surfaces as a spurious refusal that the
    user resolves by deleting the lock manually.
    """
    try:
        existing_pid = int(existing["pid"])
    except (KeyError, TypeError, ValueError):
        return True
    if not _is_pid_alive(existing_pid):
        return True
    on_disk_start = existing.get("start_time")
    live_start = _get_process_start_time(existing_pid)
    return (
        on_disk_start is not None
        and live_start is not None
        and on_disk_start != live_start
    )


def acquire_scoped_lock(
    scope: str,
    identity: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Acquire a machine-local lock for ``(scope, identity)``.

    Returns ``(True, prior_record_or_None)`` on success, ``(False,
    holding_record)`` on conflict so the caller can surface the holding
    PID to the user.

    The implementation pattern is hermes' (gateway/status.py:464) but
    trimmed to the primitives OC needs — no gateway-process recognition,
    no stopped-process detection (OC doesn't have a daemon mode where
    Ctrl+Z is the conflict source).
    """
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }

    existing = _read_lock_file(lock_path)
    if existing is None and lock_path.exists():
        # Empty / unreadable file — likely a previous process killed
        # between O_CREAT|O_EXCL and the json.dump(). Treat as stale.
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    elif existing:
        if (
            existing.get("pid") == os.getpid()
            and existing.get("start_time") == record.get("start_time")
        ):
            # Re-acquiring our own lock — common when a single process
            # connects to the same identity twice in its lifetime.
            _write_lock_record(lock_path, record)
            return True, existing
        if _is_lock_stale(existing):
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            return False, existing

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Race: another process beat us between the staleness check
        # and the create. Re-read to surface their record.
        return False, _read_lock_file(lock_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True, None


def _write_lock_record(path: Path, record: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass


def release_scoped_lock(scope: str, identity: str) -> None:
    """Release a previously-acquired lock when owned by this process.

    Idempotent — safe to call from a finally / signal handler even when
    we never actually held the lock (e.g. acquire returned False). Only
    deletes the file when the on-disk PID matches our own; locks held
    by a different process are left untouched.
    """
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_lock_file(lock_path)
    if not existing:
        return
    try:
        if int(existing["pid"]) != os.getpid():
            return
    except (KeyError, TypeError, ValueError):
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass
