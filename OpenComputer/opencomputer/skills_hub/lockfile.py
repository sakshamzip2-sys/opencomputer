"""HubLockFile — tracks installed hub skills with version + checksum.

JSON shape::

    {
      "version": 1,
      "entries": [
        {
          "identifier": "well-known/pead-screener",
          "name": "pead-screener",
          "version": "1.0.0",
          "source": "well-known",
          "install_path": "well-known/pead-screener",
          "sha256": "abc...",
          "installed_at": "2026-04-28T10:00:00+00:00"
        }
      ]
    }

Cross-platform file locking via the ``filelock`` library so concurrent
``oc skills install`` calls on the same profile do not corrupt the lockfile
on Linux/macOS/Windows alike. Atomic write via ``.tmp`` + ``replace``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

LOCKFILE_VERSION = 1


@dataclass(frozen=True, slots=True)
class LockEntry:
    identifier: str
    name: str
    version: str
    source: str
    install_path: str
    sha256: str
    installed_at: str


class HubLockFile:
    """Append/remove entries to the JSON lockfile with file-level locking."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(".flock")

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": LOCKFILE_VERSION, "entries": []}
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"lockfile is corrupt: {e}") from e
        if not isinstance(data, dict) or "entries" not in data:
            raise ValueError("lockfile is corrupt: missing 'entries' key")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def list(self) -> list[LockEntry]:
        data = self._read()
        return [LockEntry(**e) for e in data["entries"]]

    def get(self, identifier: str) -> LockEntry | None:
        for e in self.list():
            if e.identifier == identifier:
                return e
        return None

    def record_install(
        self,
        identifier: str,
        version: str,
        source: str,
        install_path: str,
        sha256: str,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            data = self._read()
            name = identifier.split("/", 1)[-1]
            entry = {
                "identifier": identifier,
                "name": name,
                "version": version,
                "source": source,
                "install_path": install_path,
                "sha256": sha256,
                "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            data["entries"] = [e for e in data["entries"] if e["identifier"] != identifier]
            data["entries"].append(entry)
            self._write(data)

    def record_uninstall(self, identifier: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            data = self._read()
            data["entries"] = [e for e in data["entries"] if e["identifier"] != identifier]
            self._write(data)
