"""Tests for RewindStore prune/clear/auto-prune + Checkpoint enhancements."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# coding-harness lives at extensions/coding-harness; add to path so tests can import.
HARNESS = Path(__file__).resolve().parents[1] / "extensions" / "coding-harness"
sys.path.insert(0, str(HARNESS))

from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]  # noqa: E402
from rewind.store import PruneReport, RewindStore  # type: ignore[import-not-found]  # noqa: E402


# ─── Checkpoint enhancement (T9) ────────────────────────────────────


def test_checkpoint_excludes_large_files() -> None:
    files = {
        "small.txt": b"x" * 10,
        "huge.bin": b"y" * 5000,
    }
    cp = Checkpoint.from_files(files, label="t", max_file_size_bytes=1000)
    assert "small.txt" in cp.files
    assert "huge.bin" not in cp.files
    assert cp.excluded_files == ("huge.bin",)


def test_checkpoint_no_max_includes_all() -> None:
    files = {"a": b"a", "b": b"bb"}
    cp = Checkpoint.from_files(files, label="t")
    assert "a" in cp.files
    assert "b" in cp.files
    assert cp.excluded_files == ()


def test_checkpoint_save_load_round_trips_excluded(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    files = {"small": b"s", "big": b"x" * 100}
    cp = Checkpoint.from_files(files, label="t", max_file_size_bytes=10)
    assert cp.excluded_files == ("big",)
    store.save(cp)
    loaded = store.load(cp.id)
    assert loaded is not None
    assert loaded.excluded_files == ("big",)
    assert "small" in loaded.files
    assert "big" not in loaded.files


# ─── size + count + oldest + newest (T10) ───────────────────────────


def test_total_size_bytes_empty(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    assert store.total_size_bytes() == 0


def test_total_size_bytes_populated(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    cp = Checkpoint.from_files({"a": b"x" * 100}, label="l")
    store.save(cp)
    assert store.total_size_bytes() >= 100


def test_count_empty(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    assert store.count() == 0


def test_count_populated(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    store.save(Checkpoint.from_files({"a": b"1"}, label="x"))
    time.sleep(0.005)
    store.save(Checkpoint.from_files({"b": b"2"}, label="y"))
    assert store.count() == 2


def test_oldest_newest_with_data(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    cp1 = Checkpoint.from_files({"a": b"1"}, label="first")
    store.save(cp1)
    time.sleep(0.01)
    cp2 = Checkpoint.from_files({"b": b"2"}, label="second")
    store.save(cp2)
    o = store.oldest()
    n = store.newest()
    assert o is not None and o.id == cp1.id
    assert n is not None and n.id == cp2.id


def test_oldest_newest_empty_returns_none(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    assert store.oldest() is None
    assert store.newest() is None


def test_total_size_includes_subagents(tmp_path: Path) -> None:
    parent = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    sub = RewindStore(tmp_path / "rw", workspace_root=tmp_path, subagent_id="s1")
    parent.save(Checkpoint.from_files({"main": b"m" * 50}, label="m"))
    sub.save(Checkpoint.from_files({"sub": b"s" * 50}, label="s"))
    parent_with_sub = parent.total_size_bytes(include_subagents=True)
    parent_only = parent.total_size_bytes(include_subagents=False)
    assert parent_with_sub > parent_only


# ─── prune (T11) ───────────────────────────────────────────────────


def test_prune_no_policy_drops_only_orphans(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    store.save(Checkpoint.from_files({"a": b"1"}, label="x"))
    orphan = store.root / "deadbeefcafebabe"
    orphan.mkdir()
    (orphan / "files").mkdir()

    report = store.prune()
    assert "deadbeefcafebabe" in report.orphans_removed
    assert report.kept == 1


def test_prune_older_than_days(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    cp = Checkpoint.from_files({"a": b"1"}, label="ancient")
    store.save(cp)
    meta = store.root / cp.id / "meta.json"
    data = json.loads(meta.read_text())
    data["created_at"] = "2020-01-01T00:00:00+00:00"
    meta.write_text(json.dumps(data))

    report = store.prune(older_than_days=7)
    assert cp.id in report.dropped
    assert store.count() == 0


def test_prune_max_count_drops_oldest(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    cps = []
    for i in range(5):
        cp = Checkpoint.from_files({f"f{i}": str(i).encode()}, label=f"l{i}")
        store.save(cp)
        cps.append(cp)
        time.sleep(0.005)

    report = store.prune(max_count=2)
    assert len(report.dropped) == 3
    assert store.count() == 2
    remaining = {c.id for c in store.list()}
    assert cps[-1].id in remaining
    assert cps[-2].id in remaining


def test_prune_max_total_bytes_drops_oldest(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    for i in range(5):
        cp = Checkpoint.from_files({f"f{i}": b"x" * 1000}, label=f"l{i}")
        store.save(cp)
        time.sleep(0.005)
    target = 2500
    store.prune(max_total_bytes=target)
    # Allow leeway for meta.json overhead per remaining checkpoint.
    assert store.total_size_bytes(include_subagents=False) <= int(target * 1.5)


def test_prune_dry_run_no_io(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    cp = Checkpoint.from_files({"a": b"1"}, label="x")
    store.save(cp)
    before = store.count()
    report = store.prune(max_count=0, dry_run=True)
    assert report.dry_run is True
    assert cp.id in report.dropped
    assert store.count() == before  # nothing actually deleted


def test_prune_pending_delete_recovers(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    pending = store.root / RewindStore.PENDING_DELETE_DIR
    pending.mkdir(parents=True)
    leftover = pending / "old"
    leftover.mkdir()
    (leftover / "x").write_text("y")

    store.prune()
    # Either pending was rmdir'd or it's now empty.
    assert not pending.exists() or not any(pending.iterdir())


# ─── clear / auto-prune (T12) ──────────────────────────────────────


def test_clear_returns_count_and_wipes(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    store.save(Checkpoint.from_files({"a": b"1"}, label="x"))
    time.sleep(0.005)
    store.save(Checkpoint.from_files({"b": b"2"}, label="y"))
    n = store.clear()
    assert n == 2
    assert store.count() == 0


def test_clear_preserves_last_prune_marker(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    store.mark_pruned()
    store.save(Checkpoint.from_files({"a": b"1"}, label="x"))
    store.clear()
    assert (store.root / RewindStore.LAST_PRUNE_MARKER).exists()


def test_should_auto_prune_first_call_true(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    assert store.should_auto_prune(min_interval_hours=24) is True


def test_should_auto_prune_within_window_false(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    store.mark_pruned()
    assert store.should_auto_prune(min_interval_hours=24) is False


def test_should_auto_prune_after_window_true(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    store.mark_pruned()
    marker = store.root / RewindStore.LAST_PRUNE_MARKER
    past = time.time() - 25 * 3600
    os.utime(marker, (past, past))
    assert store.should_auto_prune(min_interval_hours=24) is True


def test_save_evicts_oldest_when_capped(tmp_path: Path) -> None:
    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    cp_a = Checkpoint.from_files({"a": b"x" * 5000}, label="a")
    store.save(cp_a)
    time.sleep(0.005)
    cp_b = Checkpoint.from_files({"b": b"x" * 5000}, label="b")
    store.save(cp_b)
    time.sleep(0.005)

    cp_c = Checkpoint.from_files({"c": b"x" * 5000}, label="c")
    # cap forces eviction of cp_a
    store.save(cp_c, max_total_bytes=12000)
    ids = {c.id for c in store.list()}
    assert cp_a.id not in ids
    assert cp_b.id in ids
    assert cp_c.id in ids


def test_prune_report_is_typed() -> None:
    """Sanity check: PruneReport is the canonical type imported from store."""
    rep = PruneReport(
        dropped=("a",),
        kept=0,
        orphans_removed=(),
        bytes_freed=10,
        bytes_remaining=0,
        dry_run=False,
    )
    assert rep.dropped == ("a",)
