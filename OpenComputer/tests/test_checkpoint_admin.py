"""Tests for opencomputer.checkpoint_admin — Section B.5 of the spec."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# Ensure coding-harness is importable for tests
HARNESS = Path(__file__).resolve().parents[1] / "extensions" / "coding-harness"
sys.path.insert(0, str(HARNESS))

from rewind.checkpoint import Checkpoint  # type: ignore[import-not-found]  # noqa: E402
from rewind.store import RewindStore  # type: ignore[import-not-found]  # noqa: E402

from opencomputer.checkpoint_admin import (  # noqa: E402
    AggregateReport,
    PrunePolicy,
    StoreInfo,
    aggregate_status,
    clear_all,
    harness_root,
    iter_stores,
    prune_all,
)


@pytest.fixture
def harness_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override harness_root() to point under tmp_path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    return tmp_path / "harness"


def _make_session(harness: Path, sid: str, n: int = 1) -> Path:
    rw = harness / sid / "rewind"
    store = RewindStore(rw, workspace_root=harness)
    for i in range(n):
        store.save(Checkpoint.from_files({f"f{i}": b"x" * 100}, label=f"l{i}"))
        time.sleep(0.005)
    return rw


def test_iter_stores_empty(harness_dir: Path) -> None:
    assert list(iter_stores()) == []


def test_iter_stores_multiple(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=2)
    _make_session(harness_dir, "s2", n=3)
    stores = list(iter_stores())
    assert len(stores) == 2
    sids = {s.session_id for s in stores}
    assert sids == {"s1", "s2"}


def test_aggregate_status(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=2)
    rep = aggregate_status()
    assert isinstance(rep, AggregateReport)
    assert rep.total_count == 2
    assert rep.total_size_bytes > 0


def test_prune_all_session_filter(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=3)
    _make_session(harness_dir, "s2", n=3)
    out = prune_all(policy=PrunePolicy(max_count=1), session_filter="s1")
    assert "s1" in out
    assert "s2" not in out
    s1_count = sum(1 for c in (harness_dir / "s1" / "rewind").iterdir() if c.is_dir() and (c / "meta.json").exists())
    s2_count = sum(1 for c in (harness_dir / "s2" / "rewind").iterdir() if c.is_dir() and (c / "meta.json").exists())
    assert s1_count == 1
    assert s2_count == 3


def test_clear_all_session_filter(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=2)
    _make_session(harness_dir, "s2", n=2)
    n = clear_all(session_filter="s1")
    assert n == 2
    assert any(s.session_id == "s2" for s in iter_stores())


def test_harness_root_respects_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path / "x"))
    assert harness_root() == tmp_path / "x" / "harness"


def test_iter_stores_handles_unreadable_dir(harness_dir: Path) -> None:
    bad = harness_dir / "bad"
    bad.mkdir(parents=True)
    (bad / "rewind").mkdir()
    stores = list(iter_stores())
    assert len(stores) == 1
    assert stores[0].count == 0


def test_store_info_carries_subagent_count(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=1)
    # Add a subagent dir to the existing session.
    sub = harness_dir / "s1" / "rewind" / "subagents" / "sub-a"
    sub.mkdir(parents=True)
    (sub / "fakeid").mkdir()
    (sub / "fakeid" / "meta.json").write_text(
        '{"id":"fakeid","label":"x","created_at":"2026-01-01T00:00:00+00:00","paths":[]}'
    )
    (sub / "fakeid" / "files").mkdir()

    stores = list(iter_stores())
    s1 = next(s for s in stores if s.session_id == "s1")
    assert s1.subagent_count == 1
    # subagent's checkpoint folds into the count
    assert s1.count >= 2


def test_prune_policy_from_config() -> None:
    """PrunePolicy.from_config maps CheckpointsConfig fields correctly."""

    class _StubCfg:
        retention_days = 7
        max_total_size_mb = 100
        max_snapshots = 25
        delete_orphans = False

    p = PrunePolicy.from_config(_StubCfg())
    assert p.older_than_days == 7
    assert p.max_total_bytes == 100 * 1024 * 1024
    assert p.max_count == 25
    assert p.delete_orphans is False
    assert p.dry_run is False


def test_prune_all_marks_pruned(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=2)
    prune_all(policy=PrunePolicy(max_count=1))
    marker = harness_dir / "s1" / "rewind" / RewindStore.LAST_PRUNE_MARKER
    assert marker.exists()


def test_prune_all_dry_run_does_not_mark(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=2)
    prune_all(policy=PrunePolicy(max_count=1, dry_run=True))
    marker = harness_dir / "s1" / "rewind" / RewindStore.LAST_PRUNE_MARKER
    assert not marker.exists()


def test_iter_stores_yields_store_info_type(harness_dir: Path) -> None:
    _make_session(harness_dir, "s1", n=1)
    stores = list(iter_stores())
    assert isinstance(stores[0], StoreInfo)
