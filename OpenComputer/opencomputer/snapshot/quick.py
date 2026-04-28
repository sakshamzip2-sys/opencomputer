"""Quick state-snapshot machinery — Hermes Tier 2.A port.

Mirrors ``hermes_cli/backup.py:457-642`` (the ``_QUICK_*`` lane), adapted
to OpenComputer's profile-home layout. WAL-safe SQLite copy via
``sqlite3.Connection.backup()``; non-DB files via :func:`shutil.copy2`.

Layout::

    <profile_home>/state-snapshots/
        20260428-184500/                 ← snap_id (UTC ts, optional label suffix)
            manifest.json                  ← id, ts, label, file_count, total_size, files{rel→bytes}
            sessions.db
            config.yaml
            .env
            ...

Auto-prune keeps the 20 most recent. Restore is per-file overwrite, not
atomic across files (best effort). For ``.db`` files, restore uses a
temp-then-move dance so a partial write doesn't leave a corrupt DB.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.snapshot")

SNAPSHOTS_DIR = "state-snapshots"
DEFAULT_KEEP = 20

# Critical state files to include (relative to profile_home). Everything
# else is regeneratable (logs, cache, agent dreaming files) or managed
# separately (skills/, plugins/). Add more here only when there's a
# concrete restore-after-loss reason.
QUICK_STATE_FILES: tuple[str, ...] = (
    "sessions.db",
    "config.yaml",
    ".env",
    "auth-profiles.json",
    "channel_directory.json",
    "processes.json",
    "preset.yaml",
)


def snapshot_root(profile_home: Path) -> Path:
    """Return ``<profile_home>/state-snapshots/``. Caller is responsible for
    passing the resolved profile home (typically via
    :func:`opencomputer.agent.config._home`)."""
    return profile_home / SNAPSHOTS_DIR


def create_snapshot(profile_home: Path, *, label: str | None = None) -> str | None:
    """Create a quick state snapshot of critical files.

    Args:
        profile_home: The profile home directory (e.g. ``~/.opencomputer/<id>``).
        label: Optional alphanumeric tag appended to the snapshot id.

    Returns:
        The snapshot id (timestamp-based) on success; ``None`` if no
        eligible files were found.
    """
    root = snapshot_root(profile_home)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snap_id = f"{ts}-{label}" if label else ts
    snap_dir = root / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, int] = {}

    for rel in QUICK_STATE_FILES:
        src = profile_home / rel
        if not src.exists() or not src.is_file():
            continue

        dst = snap_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            if src.suffix == ".db":
                if not _safe_copy_db(src, dst):
                    continue
            else:
                shutil.copy2(src, dst)
            manifest[rel] = dst.stat().st_size
        except (OSError, PermissionError) as exc:
            logger.warning("could not snapshot %s: %s", rel, exc)

    if not manifest:
        # No files captured — clean up the empty dir so list_snapshots
        # doesn't show ghost entries.
        shutil.rmtree(snap_dir, ignore_errors=True)
        return None

    meta: dict[str, Any] = {
        "id": snap_id,
        "timestamp": ts,
        "label": label,
        "file_count": len(manifest),
        "total_size": sum(manifest.values()),
        "files": manifest,
    }
    (snap_dir / "manifest.json").write_text(json.dumps(meta, indent=2))

    _prune(root, keep=DEFAULT_KEEP)
    logger.info("snapshot created: %s (%d files, %d bytes)", snap_id, len(manifest), sum(manifest.values()))
    return snap_id


def list_snapshots(profile_home: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return the up-to-``limit`` most recent snapshots, newest first.

    Each entry is the parsed ``manifest.json`` dict. Snapshots without a
    readable manifest get a synthetic ``{id, file_count: 0, total_size: 0}``
    so the caller can still show them and decide whether to drop.
    """
    root = snapshot_root(profile_home)
    if not root.exists():
        return []

    out: list[dict[str, Any]] = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        manifest_path = d / "manifest.json"
        if manifest_path.exists():
            try:
                out.append(json.loads(manifest_path.read_text()))
            except (json.JSONDecodeError, OSError):
                out.append({"id": d.name, "file_count": 0, "total_size": 0})
        else:
            out.append({"id": d.name, "file_count": 0, "total_size": 0})
        if len(out) >= limit:
            break
    return out


def restore_snapshot(profile_home: Path, snapshot_id: str) -> int:
    """Restore state from a quick snapshot. Returns count of files restored.

    Overwrites current files. ``.db`` files use a temp-then-move sequence
    so a partial write doesn't leave the DB corrupt. **Note:** any process
    that has the DB open will continue to see the in-memory state until
    it reconnects — restoring while the agent is mid-turn is undefined
    behavior.
    """
    root = snapshot_root(profile_home)
    snap_dir = root / snapshot_id
    if not snap_dir.is_dir():
        return 0

    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return 0

    try:
        meta = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    restored = 0
    for rel in meta.get("files", {}):
        src = snap_dir / rel
        if not src.exists():
            continue

        dst = profile_home / rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            if dst.suffix == ".db":
                # Atomic-ish: copy to .name.snap_restore, then rename.
                tmp = dst.parent / f".{dst.name}.snap_restore"
                shutil.copy2(src, tmp)
                dst.unlink(missing_ok=True)
                shutil.move(str(tmp), str(dst))
            else:
                shutil.copy2(src, dst)
            restored += 1
        except (OSError, PermissionError) as exc:
            logger.error("failed to restore %s: %s", rel, exc)

    logger.info("restored %d files from snapshot %s", restored, snapshot_id)
    return restored


def prune_snapshots(profile_home: Path, *, keep: int = DEFAULT_KEEP) -> int:
    """Remove oldest snapshots beyond ``keep``. Returns count deleted."""
    return _prune(snapshot_root(profile_home), keep=keep)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _prune(root: Path, *, keep: int) -> int:
    if not root.exists():
        return 0

    dirs = sorted(
        (d for d in root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,  # newest first
    )

    deleted = 0
    for d in dirs[keep:]:
        try:
            shutil.rmtree(d)
            deleted += 1
        except OSError as exc:
            logger.warning("failed to prune snapshot %s: %s", d.name, exc)
    return deleted


def _safe_copy_db(src: Path, dst: Path) -> bool:
    """Copy a SQLite DB safely using the backup API (handles WAL mode).

    Falls back to :func:`shutil.copy2` if the backup API errors (e.g.
    not actually a SQLite DB but happens to have a ``.db`` suffix).
    """
    try:
        with sqlite3.connect(str(src)) as src_conn, sqlite3.connect(str(dst)) as dst_conn:
            src_conn.backup(dst_conn)
        return True
    except sqlite3.Error as exc:
        logger.warning("sqlite backup failed for %s, falling back to copy: %s", src, exc)
        try:
            shutil.copy2(src, dst)
            return True
        except (OSError, PermissionError) as e2:
            logger.error("fallback copy also failed for %s: %s", src, e2)
            return False
