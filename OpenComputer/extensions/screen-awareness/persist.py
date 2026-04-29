"""Opt-in JSONL append store for screen captures with TTL rotation.

Default OFF — only writes when ``enabled=True``. Each capture is one
JSON line with fields ``{captured_at, text, sha256, trigger, session_id, tool_call_id}``.
``prune()`` drops entries older than ``ttl_seconds`` via atomic rewrite
(temp file + rename).

Image bytes are NEVER persisted — text only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path

from .ring_buffer import ScreenCapture

_log = logging.getLogger("opencomputer.screen_awareness.persist")

#: Default TTL — 7 days.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


class ScreenHistoryStore:
    """JSONL-backed history with TTL rotation."""

    def __init__(
        self,
        *,
        path: Path,
        enabled: bool,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds

    def append(self, cap: ScreenCapture) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = asdict(cap)
        line = json.dumps(record, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def prune(self) -> int:
        """Drop entries older than ``ttl_seconds``. Returns count dropped."""
        if not self.path.exists():
            return 0
        cutoff = time.time() - self.ttl_seconds
        kept: list[str] = []
        dropped = 0
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    dropped += 1
                    continue
                if rec.get("captured_at", 0) < cutoff:
                    dropped += 1
                    continue
                kept.append(line)
        except OSError:
            return 0
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, self.path)
        return dropped


__all__ = ["DEFAULT_TTL_SECONDS", "ScreenHistoryStore"]
