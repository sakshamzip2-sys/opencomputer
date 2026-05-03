"""v0.5 Item: selective snapshot restore via --only / --skip."""
from __future__ import annotations

from opencomputer.snapshot.quick import (
    create_snapshot,
    list_snapshot_files,
    restore_snapshot,
)


def _seed_profile(tmp_path):
    """Make a fake profile_home with a few files."""
    (tmp_path / "config.yaml").write_text("model: opus\n")
    (tmp_path / "sessions.db").write_text("OLD-DB-CONTENT")
    (tmp_path / "feature_flags.json").write_text('{"x": 1}')
    return tmp_path


def test_list_snapshot_files_returns_manifest(tmp_path):
    p = _seed_profile(tmp_path)
    snap_id = create_snapshot(p, label="test")
    files = list_snapshot_files(p, snap_id)
    # The snapshot allowlist is a fixed set defined in quick.py — we
    # care that listing returns SOMETHING and is well-formed.
    assert isinstance(files, list)
    assert "config.yaml" in files  # always in the allowlist


def test_only_filter_restores_subset(tmp_path):
    p = _seed_profile(tmp_path)
    snap_id = create_snapshot(p, label="test")

    # Modify all three files post-snapshot
    (p / "config.yaml").write_text("model: NEW\n")
    (p / "sessions.db").write_text("NEW-DB-CONTENT")
    (p / "feature_flags.json").write_text('{"x": 999}')

    # Restore ONLY config.yaml
    n = restore_snapshot(p, snap_id, only=["config.yaml"])
    assert n == 1

    assert (p / "config.yaml").read_text() == "model: opus\n"  # restored
    assert (p / "sessions.db").read_text() == "NEW-DB-CONTENT"  # untouched
    assert (p / "feature_flags.json").read_text() == '{"x": 999}'  # untouched


def test_skip_filter_restores_complement(tmp_path):
    p = _seed_profile(tmp_path)
    snap_id = create_snapshot(p, label="test")

    (p / "config.yaml").write_text("CHANGED")
    (p / "sessions.db").write_text("CHANGED")

    # Restore everything EXCEPT config.yaml
    n = restore_snapshot(p, snap_id, skip=["config.yaml"])
    assert n >= 1
    assert (p / "config.yaml").read_text() == "CHANGED"  # untouched (skipped)
    assert (p / "sessions.db").read_text() == "OLD-DB-CONTENT"  # restored


def test_only_overrides_skip_when_both_given(tmp_path):
    """If user supplies both, ``only`` wins."""
    p = _seed_profile(tmp_path)
    snap_id = create_snapshot(p, label="test")

    (p / "config.yaml").write_text("CHANGED")
    n = restore_snapshot(
        p, snap_id, only=["config.yaml"], skip=["config.yaml"],
    )
    assert n == 1
    assert (p / "config.yaml").read_text() == "model: opus\n"


def test_full_restore_unchanged_when_no_filter(tmp_path):
    """Backward-compat: passing neither only nor skip restores everything."""
    p = _seed_profile(tmp_path)
    snap_id = create_snapshot(p, label="test")

    (p / "config.yaml").write_text("X")
    (p / "sessions.db").write_text("Y")

    n = restore_snapshot(p, snap_id)
    assert n >= 2
    assert (p / "config.yaml").read_text() == "model: opus\n"
