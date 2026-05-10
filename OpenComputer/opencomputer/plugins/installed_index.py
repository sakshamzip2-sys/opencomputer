"""Per-profile installed-plugin index.

Records the *source* of each installed plugin (catalog/git/url/path),
the verification metadata (sha256 or git ref), and the install timestamp.
This is the source of truth `oc plugin verify` uses to re-fetch and
compare bytes.

Lives at ``~/.opencomputer/<profile>/plugins/.installed_index.json`` —
hidden, JSON, one entry per installed plugin.

The file is rewritten atomically (write-tmp + rename) on every update.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstalledRecord:
    plugin_id: str
    version: str
    source: str  # "catalog" | "git" | "url" | "path"
    # Slug for catalog, git url for git, https url for url, abs path for path.
    source_url: str
    source_ref: str | None  # git sha when source=="git", else None
    tarball_sha256: str | None  # sha256 when source in ("catalog","url"), else None
    installed_at: int  # epoch seconds


def read_index(path: Path) -> list[InstalledRecord]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or "plugins" not in raw:
        return []
    out: list[InstalledRecord] = []
    for entry in raw.get("plugins", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(
                InstalledRecord(
                    plugin_id=str(entry["plugin_id"]),
                    version=str(entry.get("version", "")),
                    source=str(entry.get("source", "")),
                    source_url=str(entry.get("source_url", "")),
                    source_ref=entry.get("source_ref"),
                    tarball_sha256=entry.get("tarball_sha256"),
                    installed_at=int(entry.get("installed_at", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".idx-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_install(path: Path, record: InstalledRecord) -> None:
    """Insert or replace the record for record.plugin_id."""
    existing = [r for r in read_index(path) if r.plugin_id != record.plugin_id]
    existing.append(record)
    _atomic_write(
        path,
        {
            "schema_version": 1,
            "plugins": [
                asdict(r) for r in sorted(existing, key=lambda r: r.plugin_id)
            ],
        },
    )


def remove_install(path: Path, plugin_id: str) -> None:
    remaining = [r for r in read_index(path) if r.plugin_id != plugin_id]
    if not remaining:
        # Empty index — keep the file so callers see schema_version.
        _atomic_write(path, {"schema_version": 1, "plugins": []})
        return
    _atomic_write(
        path,
        {
            "schema_version": 1,
            "plugins": [
                asdict(r) for r in sorted(remaining, key=lambda r: r.plugin_id)
            ],
        },
    )


def find_record(path: Path, plugin_id: str) -> InstalledRecord | None:
    for r in read_index(path):
        if r.plugin_id == plugin_id:
            return r
    return None


__all__ = [
    "InstalledRecord",
    "find_record",
    "read_index",
    "record_install",
    "remove_install",
]
