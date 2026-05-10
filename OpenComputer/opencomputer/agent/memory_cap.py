"""Cap-status helper for declarative-memory files.

Single source of truth for "how full is MEMORY.md / USER.md?". Used by the Memory tool to
prepend in-band warnings to its result when a write pushes a file past the warn
threshold, and by `oc memory doctor` for human-facing reporting.

Pure module — no I/O, no imports from `opencomputer.tools.*`. Reuses the paragraph
segmentation + compaction-header detection helpers from `memory.py` so layout semantics
stay in one place.

Part of the 2026-05-10 memory-observability design (M1 milestone).
"""

from __future__ import annotations

from dataclasses import dataclass

from opencomputer.agent.memory import (
    _segment_paragraphs,
    _strip_prior_compaction_header,
)

#: Fraction of capacity at which we begin warning the agent in-band.  Below this
#: a write returns no extra signal; at or above, the Memory tool prepends a
#: warning string to its result so the next agent turn sees the pressure.
WARN_THRESHOLD: float = 0.80


@dataclass(frozen=True, slots=True)
class CapStatus:
    """Snapshot of how full a memory file is at a moment in time.

    ``pct`` may exceed 1.0 transiently — for instance when the post-replace
    candidate is computed before compaction is applied. Callers should treat
    >1.0 as "would overflow without compaction" rather than clamping.
    """

    file_name: str
    bytes_used: int
    bytes_limit: int
    pct: float
    paragraph_count: int


def cap_status(text: str, *, limit: int, file_name: str) -> CapStatus:
    """Compute :class:`CapStatus` for ``text`` against ``limit``.

    The compaction header (``## Older notes (...)``) does not count toward the
    paragraph count; it's metadata, not an entry.
    """
    # Defensive on limit=0: a misconfigured profile must not ZeroDivisionError
    # on every write. Report 0.0 and let the caller decide (warning_for with
    # limit=0 is meaningless anyway).
    bytes_used = len(text)
    pct = bytes_used / limit if limit > 0 else 0.0
    cleaned = _strip_prior_compaction_header(text).strip()
    paragraph_count = len(_segment_paragraphs(cleaned)) if cleaned else 0
    return CapStatus(
        file_name=file_name,
        bytes_used=bytes_used,
        bytes_limit=limit,
        pct=pct,
        paragraph_count=paragraph_count,
    )


def warning_for(status: CapStatus, *, dropped: int = 0) -> str | None:
    """Format a warning string for the agent / user, or None when unnecessary.

    Two trigger conditions:

    1. ``status.pct >= WARN_THRESHOLD`` — file is filling up; surface the
       pressure so the agent (and any human watching stderr) knows.
    2. ``dropped > 0`` — a compaction just happened; the agent NEEDS to know
       even if post-write pct is comfortable, because load-bearing entries
       may have been lost.

    Returns ``None`` only when both conditions are false.
    """
    pct_int = int(round(status.pct * 100))

    if dropped > 0:
        # Compaction event — most important signal. Mention drop count + the
        # post-write %, plus the recovery hint.
        plural = "ies" if dropped != 1 else "y"
        return (
            f"\U0001F6D1 MEMORY {status.file_name} COMPACTED — "
            f"DROPPED {dropped} ENTR{plural} "
            f"(post-write {pct_int}%, {status.bytes_used}/{status.bytes_limit} chars). "
            "Run `oc memory audit` to review what was kept; check `.bak` or `git log` "
            "for what was dropped."
        )

    if status.pct >= WARN_THRESHOLD:
        return (
            f"⚠️ MEMORY {status.file_name} AT {pct_int}% "
            f"({status.bytes_used}/{status.bytes_limit} chars). "
            "Consider `Memory(action='remove', ...)` for stale entries, or run "
            "`oc memory audit` to review."
        )

    return None
