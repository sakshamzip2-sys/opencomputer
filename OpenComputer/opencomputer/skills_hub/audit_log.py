"""Append-only JSONL audit log for Skills Hub install/uninstall/update events.

Co-located with the lockfile under
``~/.opencomputer/<profile>/skills/.hub/audit.log``. Each line is a JSON
object with at minimum: ``timestamp``, ``action``, ``identifier``, ``source``.
``install`` events also carry ``version`` + ``verdict`` (skills_guard scan
result).

This log is human-readable + machine-parseable. Intentionally NOT
HMAC-chained like the F1 consent audit (different threat model — this is
"what got installed" not "did the agent bypass consent"). Cannot be
truncated by code paths that don't import this module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ALLOWED_ACTIONS = ("install", "uninstall", "update", "scan_blocked")


class AuditLog:
    """Append-only JSONL log."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def record(self, action: str, identifier: str, source: str, **extra: Any) -> None:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(
                f"unknown action {action!r}; expected one of {ALLOWED_ACTIONS}"
            )
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "action": action,
            "identifier": identifier,
            "source": source,
            **extra,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def entries(self, action: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if action is not None and entry.get("action") != action:
                continue
            out.append(entry)
        return out
