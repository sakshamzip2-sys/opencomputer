"""Append-only MRU store for the slash dropdown.

Tracks the user's last-50 distinct slash picks (commands or skills) so
``UnifiedSlashSource.rank`` can boost recently-used items by a small
score bonus. Persisted to ``<profile_home>/slash_mru.json`` so the
ranking carries across sessions.

Tolerant of corruption + missing-file: any read error returns an empty
in-memory store; the next ``record`` call rewrites the file from
scratch. Atomic writes via temp-file + rename so a crash mid-write
never leaves a half-written file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Cap on stored entries — Claude Code uses a similar bounded LRU for
#: command frequency. 50 is enough to bias toward the user's last
#: working session without growing unbounded.
_MAX_ENTRIES = 50

#: Score bonus added to a ranked match when the item appears in the
#: MRU log. Spec §3.4. Additive, capped at 1.0 by the caller.
RECENCY_BONUS = 0.05


class MruStore:
    """Persistent bounded most-recently-used log of slash picks.

    Public API:
    - ``record(name)`` — log a pick; rewrites the JSON file atomically.
    - ``recency_bonus(name) -> float`` — returns ``RECENCY_BONUS`` if
      ``name`` is in the last-``_MAX_ENTRIES`` log, ``0.0`` otherwise.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        # Defensive — drop entries that don't have ``name``.
        return [e for e in data if isinstance(e, dict) and "name" in e]

    def record(self, name: str) -> None:
        # Drop any prior occurrence so the new one becomes most-recent.
        self._entries = [e for e in self._entries if e.get("name") != name]
        self._entries.append({"name": name, "ts": time.time()})
        # Trim to last ``_MAX_ENTRIES`` (oldest at front).
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._entries), encoding="utf-8")
        os.replace(tmp, self.path)

    def recency_bonus(self, name: str) -> float:
        return RECENCY_BONUS if any(e.get("name") == name for e in self._entries) else 0.0


__all__ = ["MruStore", "RECENCY_BONUS"]
