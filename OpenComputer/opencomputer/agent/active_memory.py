"""Active Memory — proactive local recall prepend (OpenClaw 1.B-alt port).

OpenClaw's "active-memory" plugin runs a bounded sub-agent before each reply.
This OC implementation is a much simpler RecallTool-prepend pattern: on every
eligible turn, query the local episodic + message FTS5 indices for recent
context that matches the user's most recent message, and prepend the top-N
hits to the system prompt as a `<relevant-memories>` block.

Differences vs RecallTool (which the model decides to call):
  - This injector ALWAYS runs (when enabled) — no model decision required.
  - It returns a compact text block, never a tool result.
  - It only READS — writes stay with reviewer.py post-hoc capture.

Differences vs Honcho memory_bridge (already in agent/loop.py):
  - Honcho hits the external memory provider over the network.
  - This one hits the local SessionDB FTS5 indices — works offline, free.
  - The two compose: both append to the per-turn ``system`` lane.

Enable via config.memory.active_memory_enabled = True (default off).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _SearchableDB(Protocol):
    def search_episodic(self, query: str, *, limit: int = 10): ...
    def search(self, query: str, *, limit: int = 10): ...


@dataclass(frozen=True, slots=True)
class ActiveMemoryConfig:
    enabled: bool = False
    top_n: int = 3
    min_query_chars: int = 3


class ActiveMemoryInjector:
    """Prepends a `<relevant-memories>` block when local FTS5 hits exist."""

    def __init__(self, db: _SearchableDB, *, config: ActiveMemoryConfig | None = None) -> None:
        self._db = db
        self._cfg = config or ActiveMemoryConfig()

    def recall_block(self, query: str) -> str | None:
        """Return a `<relevant-memories>` block if hits exist; else None.

        Caller appends the returned string to the per-turn ``system`` so the
        prompt-prefix cache stays warm across turns.
        """
        if not self._cfg.enabled:
            return None
        q = (query or "").strip()
        if len(q) < self._cfg.min_query_chars:
            return None

        try:
            episodic = self._db.search_episodic(q, limit=self._cfg.top_n)
        except Exception:
            episodic = []
        try:
            remaining = max(0, self._cfg.top_n - len(episodic))
            messages = self._db.search(q, limit=remaining) if remaining else []
        except Exception:
            messages = []

        if not episodic and not messages:
            return None

        lines = ["<relevant-memories>"]
        for h in episodic:
            sid = (h.get("session_id") or "")[:8]
            turn = h.get("turn_index", "?")
            summary = h.get("summary") or ""
            lines.append(f"[ep {sid}…/{turn}] {summary}")
        for h in messages:
            sid = (h.get("session_id") or "")[:8]
            role = h.get("role", "?")
            snippet = h.get("snippet") or ""
            lines.append(f"[msg {sid}… {role}] {snippet}")
        lines.append("</relevant-memories>")
        return "\n".join(lines)


__all__ = ["ActiveMemoryInjector", "ActiveMemoryConfig"]
