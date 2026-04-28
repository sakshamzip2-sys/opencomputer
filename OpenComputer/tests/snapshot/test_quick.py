"""Tests for the opencomputer.snapshot quick state-snapshot module.

Hermes Tier 2.A port — mirrors hermes_cli/backup.py:457-642 logic but
adapted to OC's profile_home layout.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from opencomputer.snapshot import (
    DEFAULT_KEEP,
    QUICK_STATE_FILES,
    create_snapshot,
    list_snapshots,
    prune_snapshots,
    restore_snapshot,
    snapshot_root,
)


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    """A populated profile_home with the well-known state files."""
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("model: claude-sonnet-4-6\n")
    (home / ".env").write_text("ANTHROPIC_API_KEY=test\n")
    (home / "auth-profiles.json").write_text("{}\n")
    # Build a real SQLite DB
    db = sqlite3.connect(str(home / "sessions.db"))
    db.execute("CREATE TABLE x (n INTEGER)")
    db.execute("INSERT INTO x VALUES (42)")
    db.commit()
    db.close()
    return home


# ---------------------------------------------------------------------------
# create_snapshot
# ---------------------------------------------------------------------------


def test_create_basic(profile: Path):
    sid = create_snapshot(profile)
    assert sid is not None
    assert (snapshot_root(profile) / sid / "manifest.json").exists()
    assert (snapshot_root(profile) / sid / "config.yaml").exists()
    assert (snapshot_root(profile) / sid / ".env").exists()
    assert (snapshot_root(profile) / sid / "sessions.db").exists()


def test_create_with_label(profile: Path):
    sid = create_snapshot(profile, label="pre-experiment")
    assert sid is not None
    assert sid.endswith("-pre-experiment")


def test_create_manifest_shape(profile: Path):
    sid = create_snapshot(profile, label="x")
    meta = json.loads((snapshot_root(profile) / sid / "manifest.json").read_text())
    assert meta["id"] == sid
    assert meta["label"] == "x"
    assert meta["file_count"] >= 3
    assert meta["total_size"] > 0
    assert "config.yaml" in meta["files"]
    assert ".env" in meta["files"]


def test_create_empty_profile_returns_none(tmp_path: Path):
    """No eligible files → returns None and cleans up the empty dir."""
    home = tmp_path / "empty"
    home.mkdir()
    sid = create_snapshot(home)
    assert sid is None
    # No leftover snapshot dirs.
    assert not snapshot_root(home).exists() or not any(snapshot_root(home).iterdir())


def test_create_db_via_backup_api(profile: Path):
    """Verify the SQLite backup API was used (DB is queryable in the snapshot)."""
    sid = create_snapshot(profile)
    snap_db_path = snapshot_root(profile) / sid / "sessions.db"
    db = sqlite3.connect(str(snap_db_path))
    rows = db.execute("SELECT n FROM x").fetchall()
    db.close()
    assert rows == [(42,)]


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------


def test_list_empty(tmp_path: Path):
    home = tmp_path / "p"
    home.mkdir()
    assert list_snapshots(home) == []


def test_list_newest_first(profile: Path):
    sid1 = create_snapshot(profile, label="first")
    # Force a different timestamp by tweaking the dir name
    import time as _t
    _t.sleep(1)  # pragma: no cover — ensures a >=1s gap
    sid2 = create_snapshot(profile, label="second")
    items = list_snapshots(profile)
    assert items[0]["id"] == sid2
    assert items[1]["id"] == sid1


def test_list_limit(profile: Path):
    create_snapshot(profile, label="a")
    create_snapshot(profile, label="b")
    items = list_snapshots(profile, limit=1)
    assert len(items) == 1


# ---------------------------------------------------------------------------
# restore_snapshot
# ---------------------------------------------------------------------------


def test_restore_basic(profile: Path):
    sid = create_snapshot(profile, label="backup")
    # Mutate config after snapshot
    (profile / "config.yaml").write_text("model: changed\n")
    n = restore_snapshot(profile, sid)
    assert n >= 3
    # Original content restored
    assert "claude-sonnet-4-6" in (profile / "config.yaml").read_text()


def test_restore_unknown_id_returns_zero(profile: Path):
    assert restore_snapshot(profile, "nonexistent-id") == 0


def test_restore_db_atomicish(profile: Path):
    sid = create_snapshot(profile)
    # Mutate the DB
    db = sqlite3.connect(str(profile / "sessions.db"))
    db.execute("INSERT INTO x VALUES (99)")
    db.commit()
    db.close()
    # Restore
    restore_snapshot(profile, sid)
    # Original DB content (only n=42) restored
    db = sqlite3.connect(str(profile / "sessions.db"))
    rows = db.execute("SELECT n FROM x").fetchall()
    db.close()
    assert rows == [(42,)]


# ---------------------------------------------------------------------------
# prune_snapshots
# ---------------------------------------------------------------------------


def test_prune_below_cap_no_op(profile: Path):
    create_snapshot(profile)
    n = prune_snapshots(profile, keep=20)
    assert n == 0


def test_prune_drops_oldest(profile: Path):
    # Create 22 snapshots with distinct timestamps (manually crafted dirs to
    # avoid 1-second sleeps in tests).
    root = snapshot_root(profile)
    root.mkdir(parents=True, exist_ok=True)
    for i in range(22):
        d = root / f"20260428-{i:06d}"
        d.mkdir()
        (d / "manifest.json").write_text(
            json.dumps({"id": d.name, "file_count": 0, "total_size": 0})
        )
    deleted = prune_snapshots(profile, keep=20)
    assert deleted == 2
    remaining = list(root.iterdir())
    assert len(remaining) == 20


def test_create_auto_prunes(profile: Path):
    """create_snapshot calls _prune internally; verify the cap is enforced."""
    root = snapshot_root(profile)
    root.mkdir(parents=True, exist_ok=True)
    # Pre-populate to cap-1.
    for i in range(DEFAULT_KEEP - 1):
        d = root / f"20260101-{i:06d}"
        d.mkdir()
        (d / "manifest.json").write_text(
            json.dumps({"id": d.name, "file_count": 0, "total_size": 0})
        )
    # New snapshot should bring us to cap exactly (no prune yet).
    create_snapshot(profile, label="a")
    assert len(list(root.iterdir())) == DEFAULT_KEEP
    # Another snapshot triggers prune of the oldest.
    create_snapshot(profile, label="b")
    items = list_snapshots(profile)
    assert len(items) == DEFAULT_KEEP


# ---------------------------------------------------------------------------
# QUICK_STATE_FILES contract
# ---------------------------------------------------------------------------


def test_quick_state_files_includes_required():
    assert "sessions.db" in QUICK_STATE_FILES
    assert "config.yaml" in QUICK_STATE_FILES
    assert ".env" in QUICK_STATE_FILES
