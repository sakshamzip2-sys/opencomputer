"""Layer 3 — Background Deepening orchestrator.

Idle-throttled loop that progressively widens the time window over which
Layer 2 sources are ingested. Cursor persistence makes the loop
resumable across crashes / reboots.

Window progression (in days, 0 = all-time):
    7 → 30 → 90 → 365 → 0 (all-time)

Each ``run_deepening()`` call processes ONE window, advances the cursor,
and returns. Caller loops at their own cadence (e.g., every 5 minutes
in a daemon, or once per CLI invocation).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from opencomputer.profile_bootstrap.idle import check_idle
from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif
from opencomputer.profile_bootstrap.recent_scan import scan_git_log, scan_recent_files

_log = logging.getLogger("opencomputer.profile_bootstrap.deepening")

#: Window progression: 7d → 30d → 90d → 365d → all-time (0).
DEFAULT_WINDOWS: tuple[int, ...] = (7, 30, 90, 365, 0)


@dataclass(frozen=True, slots=True)
class DeepeningCursor:
    """Persistent state for the deepening loop."""

    last_window_days: int = 0
    last_started_at: float = 0.0
    completed_windows: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DeepeningResult:
    """Outcome of one ``run_deepening`` call."""

    window_processed_days: int = 0
    artifacts_processed: int = 0
    motifs_emitted: int = 0
    elapsed_seconds: float = 0.0
    skipped_reason: str = ""


def _default_cursor_path() -> Path:
    from opencomputer.agent.config import _home
    return _home() / "profile_bootstrap" / "deepening_cursor.json"


def save_cursor(cursor: DeepeningCursor, *, path: Path | None = None) -> None:
    """Atomically write the cursor JSON."""
    p = path if path is not None else _default_cursor_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_window_days": cursor.last_window_days,
        "last_started_at": cursor.last_started_at,
        "completed_windows": list(cursor.completed_windows),
    }
    p.write_text(json.dumps(payload))


def load_cursor(*, path: Path | None = None) -> DeepeningCursor:
    """Read the cursor JSON. Returns default cursor if missing/corrupt."""
    p = path if path is not None else _default_cursor_path()
    if not p.exists():
        return DeepeningCursor()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return DeepeningCursor()
    return DeepeningCursor(
        last_window_days=int(data.get("last_window_days", 0)),
        last_started_at=float(data.get("last_started_at", 0.0)),
        completed_windows=tuple(int(w) for w in data.get("completed_windows", [])),
    )


def _next_window(cursor: DeepeningCursor) -> int:
    """Pick the next window in the progression that hasn't been completed."""
    for w in DEFAULT_WINDOWS:
        if w not in cursor.completed_windows:
            return w
    # All windows complete → return all-time as a no-op cycle.
    return 0


def run_deepening(
    *,
    cursor_path: Path | None = None,
    scan_roots: list[Path] | None = None,
    git_repos: list[Path] | None = None,
    max_artifacts_per_window: int = 500,
    force: bool = False,
) -> DeepeningResult:
    """Run ONE deepening pass over the next window in the progression.

    With ``force=False`` (default), short-circuits if the system is not idle.
    With ``force=True``, ignores idle detection — useful for the
    ``opencomputer profile deepen`` CLI invocation.
    """
    started = time.monotonic()

    if not force:
        status = check_idle()
        if not status.idle:
            return DeepeningResult(skipped_reason=status.reason)

    cursor = load_cursor(path=cursor_path)
    window = _next_window(cursor)
    days = window if window > 0 else 365 * 10  # 0 → "all-time" approximated as 10 years

    files = scan_recent_files(
        roots=scan_roots or [], days=days, max_files=max_artifacts_per_window,
    )
    commits = scan_git_log(
        repo_paths=git_repos or [], days=days, max_per_repo=max_artifacts_per_window,
    )

    artifacts_processed = 0
    motifs_emitted = 0

    # Process files (we have the path; LLM gets a brief content sample).
    for f in files:
        artifacts_processed += 1
        try:
            sample = Path(f.path).read_text(errors="replace")[:4000]
        except (OSError, UnicodeDecodeError):
            continue
        if extract_and_emit_motif(
            content=sample, kind="file", source_path=f.path,
        ):
            motifs_emitted += 1

    # Process git commits (subject is the content).
    for c in commits:
        artifacts_processed += 1
        if extract_and_emit_motif(
            content=c.subject, kind="git_commit", source_path=c.repo_path,
        ):
            motifs_emitted += 1

    # Advance cursor.
    new_completed = tuple(sorted({*cursor.completed_windows, window}))
    new_cursor = DeepeningCursor(
        last_window_days=window,
        last_started_at=time.time(),
        completed_windows=new_completed,
    )
    save_cursor(new_cursor, path=cursor_path)

    return DeepeningResult(
        window_processed_days=window,
        artifacts_processed=artifacts_processed,
        motifs_emitted=motifs_emitted,
        elapsed_seconds=time.monotonic() - started,
        skipped_reason="",
    )
