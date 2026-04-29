"""Tests for snapshot export + import (Sub-project E)."""
from __future__ import annotations

import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from opencomputer.snapshot.quick import (
    create_snapshot,
    export_snapshot,
    import_snapshot,
    list_snapshots,
    snapshot_root,
)


@pytest.fixture
def profile_home(tmp_path: Path) -> Path:
    """Build a minimal profile_home with critical state files seeded."""
    home = tmp_path / "profile"
    home.mkdir()
    # SQLite-magic-bytes prefix so create_snapshot's _safe_copy_db happy-path
    # uses backup() (or falls back to copy2 — either way it copies).
    (home / "sessions.db").write_bytes(b"SQLite format 3\x00fake-db-content")
    (home / "config.yaml").write_text("provider: anthropic\nmodel: claude-opus-4-7\n")
    return home


# ── export ────────────────────────────────────────────────────────────


def test_export_creates_tar_gz(profile_home, tmp_path):
    sid = create_snapshot(profile_home, label="export-test")
    assert sid is not None
    dest = tmp_path / "exported.tar.gz"
    out = export_snapshot(profile_home, sid, dest_path=dest)
    assert out == dest
    assert dest.exists()
    assert dest.stat().st_size > 0
    with tarfile.open(dest, "r:gz") as tf:
        names = tf.getnames()
        assert any("config.yaml" in n for n in names) or any(
            "sessions.db" in n for n in names
        )


def test_export_default_dest_path(profile_home):
    sid = create_snapshot(profile_home, label="default-dest")
    assert sid is not None
    out = export_snapshot(profile_home, sid)
    try:
        assert out.exists()
        assert out.suffix == ".gz"
        assert out.parent == Path.home()
        assert sid in out.name
    finally:
        if out.exists():
            out.unlink()


def test_export_unknown_snapshot_raises(profile_home, tmp_path):
    with pytest.raises(ValueError, match="not found"):
        export_snapshot(
            profile_home,
            "nonexistent-id",
            dest_path=tmp_path / "out.tar.gz",
        )


# ── import ────────────────────────────────────────────────────────────


def test_import_round_trip(profile_home, tmp_path):
    """Export then import — listing on the destination profile shows the imported one."""
    sid = create_snapshot(profile_home, label="round-trip")
    archive = export_snapshot(profile_home, sid, dest_path=tmp_path / "x.tar.gz")

    other = tmp_path / "other-profile"
    other.mkdir()
    new_id = import_snapshot(other, archive_path=archive, label="imported")
    assert new_id is not None
    listed = list_snapshots(other, limit=10)
    # After import, manifest is rewritten so id matches the new local id.
    assert any(row.get("id") == new_id for row in listed)
    # Audit trail preserved
    assert any(row.get("imported_from") == sid for row in listed)


def test_import_archive_not_found(tmp_path):
    with pytest.raises(ValueError, match="archive not found"):
        import_snapshot(
            tmp_path,
            archive_path=tmp_path / "doesnotexist.tar.gz",
            label="x",
        )


def test_import_corrupt_archive_raises(profile_home, tmp_path):
    bad = tmp_path / "corrupt.tar.gz"
    bad.write_bytes(b"not a real tarball")
    with pytest.raises((tarfile.TarError, OSError)):
        import_snapshot(profile_home, archive_path=bad, label="corrupt")


def test_import_rejects_symlink_member(profile_home, tmp_path):
    """Defense-in-depth: explicit reject on SYMTYPE before data_filter."""
    bad = tmp_path / "with_symlink.tar.gz"
    with tarfile.open(bad, "w:gz") as tf:
        # Top-level dir entry (so the strip-top logic has something to strip)
        info_dir = tarfile.TarInfo(name="topdir/")
        info_dir.type = tarfile.DIRTYPE
        tf.addfile(info_dir)
        # Symlink member
        sym = tarfile.TarInfo(name="topdir/symlink-here")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "../../../etc/passwd"
        tf.addfile(sym)
    with pytest.raises(ValueError, match="unsafe member type"):
        import_snapshot(profile_home, archive_path=bad, label="bad-sym")


def test_import_rejects_hardlink_member(profile_home, tmp_path):
    bad = tmp_path / "with_hardlink.tar.gz"
    with tarfile.open(bad, "w:gz") as tf:
        info_dir = tarfile.TarInfo(name="topdir/")
        info_dir.type = tarfile.DIRTYPE
        tf.addfile(info_dir)
        info_real = tarfile.TarInfo(name="topdir/real.txt")
        info_real.size = 4
        tf.addfile(info_real, BytesIO(b"data"))
        link = tarfile.TarInfo(name="topdir/hardlink")
        link.type = tarfile.LNKTYPE
        link.linkname = "../../../etc/passwd"
        tf.addfile(link)
    with pytest.raises(ValueError, match="unsafe member type"):
        import_snapshot(profile_home, archive_path=bad, label="bad-link")


def test_import_label_sanitized(profile_home, tmp_path):
    """Label characters are filtered down to alnum + - + _."""
    sid = create_snapshot(profile_home, label="sanitize-test")
    archive = export_snapshot(profile_home, sid, dest_path=tmp_path / "x.tar.gz")

    new_id = import_snapshot(
        profile_home,
        archive_path=archive,
        label="my/dirty\\label;rm-rf",
    )
    # Slashes, backslashes, semicolons are stripped — only alnum/-/_ survives
    assert "/" not in new_id
    assert "\\" not in new_id
    assert ";" not in new_id
