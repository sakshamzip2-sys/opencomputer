"""Process-local tracker for "have we Read this file?".

Edit/MultiEdit consult this set before mutating a file: if the agent
hasn't Read the path at least once in the current process, the edit
fails with a nudge-text error pointing the model toward the missing
Read call.

This is intentionally a simple module-level set (not session-keyed):
- The agent loop runs in a single process per turn.
- Tests that reset state between cases can call ``reset()``.
- Tracking is keyed by the resolved absolute path so symlinks and
  ``./foo``-vs-``foo`` variants collapse to the same key.

If we ever need cross-process or cross-session tracking, swap the
backing store here without changing call sites.
"""

from __future__ import annotations

from pathlib import Path

_READ_PATHS: set[str] = set()


def _key(path: str | Path) -> str:
    """Resolve to an absolute string key. ``resolve()`` collapses
    symlinks and normalises ``..``. We deliberately do NOT require
    the file to exist — the tracker just records what was *attempted*."""
    try:
        return str(Path(path).resolve())
    except Exception:
        # Fall back to the raw string if resolution fails for any
        # reason (e.g. permission errors on parent traversal).
        return str(path)


def mark_read(path: str | Path) -> None:
    """Record that the agent has Read this path."""
    _READ_PATHS.add(_key(path))


def has_been_read(path: str | Path) -> bool:
    """Return True iff ``path`` was marked as read in this process."""
    return _key(path) in _READ_PATHS


def reset() -> None:
    """Test-only: clear all tracked reads."""
    _READ_PATHS.clear()


__all__ = ["mark_read", "has_been_read", "reset"]
