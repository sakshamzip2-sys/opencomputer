"""Tests for the slash MRU store — bounded recent-use log used to
boost recently-picked items in the dropdown ranking."""
from __future__ import annotations

import json
import time
from pathlib import Path

from opencomputer.cli_ui.slash_mru import MruStore


def test_record_and_recency_bonus(tmp_path: Path) -> None:
    store = MruStore(tmp_path / "mru.json")
    store.record("rename")
    assert store.recency_bonus("rename") == 0.05
    assert store.recency_bonus("not-recorded") == 0.0


def test_persists_across_instances(tmp_path: Path) -> None:
    p = tmp_path / "mru.json"
    MruStore(p).record("reload")
    fresh = MruStore(p)
    assert fresh.recency_bonus("reload") == 0.05


def test_cap_at_50_drops_oldest(tmp_path: Path) -> None:
    store = MruStore(tmp_path / "mru.json")
    # Record 60 distinct entries; first 10 should be evicted.
    for i in range(60):
        store.record(f"cmd-{i:02d}")
    assert store.recency_bonus("cmd-00") == 0.0  # evicted
    assert store.recency_bonus("cmd-09") == 0.0  # evicted
    assert store.recency_bonus("cmd-10") == 0.05  # kept
    assert store.recency_bonus("cmd-59") == 0.05  # kept


def test_duplicate_record_refreshes_recency(tmp_path: Path) -> None:
    store = MruStore(tmp_path / "mru.json")
    store.record("a")
    time.sleep(0.001)
    store.record("a")  # second time — should not duplicate the entry
    raw = json.loads((tmp_path / "mru.json").read_text())
    assert sum(1 for e in raw if e["name"] == "a") == 1


def test_malformed_file_silently_empty(tmp_path: Path) -> None:
    p = tmp_path / "mru.json"
    p.write_text("{not valid json")
    store = MruStore(p)
    # Reading must not raise; bonus is zero.
    assert store.recency_bonus("anything") == 0.0
    # Recording must work — overwrites the bad file.
    store.record("x")
    assert store.recency_bonus("x") == 0.05


def test_missing_file_silently_empty(tmp_path: Path) -> None:
    p = tmp_path / "does-not-exist.json"
    store = MruStore(p)
    assert store.recency_bonus("anything") == 0.0
    store.record("created")
    assert p.exists()


def test_atomic_write_via_tempfile(tmp_path: Path) -> None:
    """During write, the .tmp file lands first, then is renamed."""
    store = MruStore(tmp_path / "mru.json")
    store.record("first")
    # No leftover .tmp file after a successful write.
    assert not (tmp_path / "mru.json.tmp").exists()
