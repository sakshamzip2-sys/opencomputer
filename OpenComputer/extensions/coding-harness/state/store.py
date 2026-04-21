"""SessionStateStore — JSON-backed per-session key/value + mark-once flags."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SessionStateStore:
    """Small JSON store under <root>/state.json + <root>/marks.json."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._state_path = self.root / "state.json"
        self._marks_path = self.root / "marks.json"

    # ─── internal helpers ───────────────────────────────────────

    def _read(self, path: Path) -> dict:
        if path.exists():
            txt = path.read_text() or "{}"
            return json.loads(txt)
        return {}

    def _write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data))

    # ─── key/value ──────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        return self._read(self._state_path).get(key, default)

    def set(self, key: str, value: Any) -> None:
        data = self._read(self._state_path)
        data[key] = value
        self._write(self._state_path, data)

    # ─── mark-once (dedup for "nag about rule X" style hooks) ──

    def mark_once(self, key: str) -> bool:
        """Return True the first time `key` is marked; False if already marked."""
        data = self._read(self._marks_path)
        if key in data:
            return False
        data[key] = True
        self._write(self._marks_path, data)
        return True

    def is_marked(self, key: str) -> bool:
        return key in self._read(self._marks_path)


__all__ = ["SessionStateStore"]
