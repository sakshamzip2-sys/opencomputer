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
from datetime import UTC, datetime
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
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
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


def export_snapshot(
    profile_home: Path,
    snapshot_id: str,
    *,
    dest_path: Path | None = None,
) -> Path:
    """Tar.gz a snapshot directory for migration / backup.

    Args:
        profile_home: profile root containing ``state-snapshots/<id>/``.
        snapshot_id: id returned by :func:`create_snapshot`.
        dest_path: where to write the archive. Default
            ``~/oc-snapshot-<id>-<unix-ts>.tar.gz``.

    Returns:
        Path to the created archive.

    Raises:
        ValueError: if the snapshot id is not found in this profile.
    """
    import tarfile
    import time as _time

    src = snapshot_root(profile_home) / snapshot_id
    if not src.is_dir():
        raise ValueError(
            f"snapshot {snapshot_id!r} not found in {profile_home}"
        )
    if dest_path is None:
        ts = int(_time.time())
        dest_path = Path.home() / f"oc-snapshot-{snapshot_id}-{ts}.tar.gz"
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest_path, "w:gz") as tf:
        tf.add(str(src), arcname=snapshot_id)
    return dest_path


def import_snapshot(
    profile_home: Path,
    *,
    archive_path: Path,
    label: str | None = None,
) -> str:
    """Extract a snapshot archive into ``<profile_home>/state-snapshots/<new_id>/``.

    Generates a fresh id (so importing the same archive twice doesn't
    collide). Pre-screens tarball members for special types
    (symlinks/hard-links/devices/FIFOs) and uses Python 3.12+'s
    ``tarfile.data_filter`` to defend against tar-slip and absolute paths.

    Returns:
        The new snapshot id.

    Raises:
        ValueError: archive not found, unsafe member types, or extraction
            target collides on uuid (very rare).
        tarfile.TarError, OSError: corrupt archive.
    """
    import tarfile
    import uuid

    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise ValueError(f"archive not found: {archive_path}")

    new_id = uuid.uuid4().hex[:12]
    if label:
        # Limit label length so directory names stay readable + filesystem-safe.
        safe_label = "".join(c for c in label[:40] if c.isalnum() or c in "-_")
        if safe_label:
            new_id = f"{new_id}-{safe_label}"
    dest = snapshot_root(profile_home) / new_id
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # Astronomically rare; retry with a fresh uuid.
        new_id = uuid.uuid4().hex[:12]
        if label:
            safe_label = "".join(c for c in label[:40] if c.isalnum() or c in "-_")
            if safe_label:
                new_id = f"{new_id}-{safe_label}"
        dest = snapshot_root(profile_home) / new_id
        dest.mkdir(parents=True, exist_ok=False)

    with tarfile.open(archive_path, "r:gz") as tf:
        # Defense-in-depth: reject special-type members on top of data_filter.
        for member in tf.getmembers():
            if member.type in (
                tarfile.SYMTYPE,
                tarfile.LNKTYPE,
                tarfile.CHRTYPE,
                tarfile.BLKTYPE,
                tarfile.FIFOTYPE,
                tarfile.CONTTYPE,
            ):
                # Clean up partial dest before raising.
                try:
                    shutil.rmtree(dest)
                except OSError:
                    pass
                raise ValueError(
                    f"unsafe member type {member.type!r} in archive — refusing"
                )
        # Strip top-level dir injected by export_snapshot (arcname=snapshot_id).
        members_to_extract: list[tarfile.TarInfo] = []
        for m in tf.getmembers():
            stripped = m.name.split("/", 1)
            if len(stripped) != 2:
                # Top-level dir entry itself — skip; we already created dest.
                continue
            m.name = stripped[1]
            members_to_extract.append(m)
        # Python 3.12+ data_filter handles tar-slip + absolute paths + PAX
        # tricks + Windows-style paths automatically.
        tf.extractall(
            path=str(dest), members=members_to_extract, filter="data"
        )

    # Rewrite the manifest's `id` to the new local id so list_snapshots
    # surfaces the imported snapshot under the id we just minted.
    manifest = dest / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text())
            original_id = data.get("id")
            data["id"] = new_id
            if original_id:
                data["imported_from"] = original_id
            manifest.write_text(json.dumps(data, indent=2))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "import: could not rewrite manifest for %s: %s", new_id, exc
            )

    return new_id


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
