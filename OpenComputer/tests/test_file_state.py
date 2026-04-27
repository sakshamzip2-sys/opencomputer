"""Tests for opencomputer.tools.file_state — sibling-write staleness guard.

The four failure modes covered:

1. Sibling write after my read.
2. External mtime drift since my read.
3. Write-without-read.
4. Disabled-by-env: opt-out for tests / debugging.

Plus the ``writes_since`` helper that delegate.py uses to surface a
"subagent X modified files you read earlier" reminder on the parent.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from opencomputer.tools import file_state


@pytest.fixture(autouse=True)
def reset_registry():
    """Each test starts with a fresh registry — global state would
    otherwise let one test pollute the next."""
    file_state.get_registry().clear()
    yield
    file_state.get_registry().clear()


# ──────────────────────────── basic accounting ────────────────────────────


def test_record_read_no_existing_writer_no_warning(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("orig\n")
    file_state.record_read(p, task_id="t1")
    # Same task writes its own read — should not warn.
    assert file_state.check_stale(p, task_id="t1") is None


def test_write_without_read_warns(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("orig\n")
    # Sibling task writes the file first.
    file_state.note_write(p, task_id="sibling")
    # Then a different task tries to write — they never read it.
    warning = file_state.check_stale(p, task_id="t2")
    assert warning is not None
    assert "never read" in warning.lower() or "not read" in warning.lower()


def test_sibling_write_after_my_read_warns(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("orig\n")
    # I read first.
    file_state.record_read(p, task_id="me")
    time.sleep(0.01)  # ensure timestamp ordering
    # Sibling writes after.
    p.write_text("sibling-overwrote\n")
    file_state.note_write(p, task_id="sibling")
    # I now check — should warn that sibling wrote after my read.
    warning = file_state.check_stale(p, task_id="me")
    assert warning is not None
    assert "sibling" in warning.lower()


def test_external_mtime_drift_warns(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("orig\n")
    file_state.record_read(p, task_id="me")
    time.sleep(0.01)
    # External edit (no record_read / note_write — simulates user's
    # editor or a linter touching the file outside the agent).
    p.write_text("changed by external editor\n")
    # Force mtime change to be observable on coarse-resolution filesystems.
    new_mtime = time.time() + 5
    import os
    os.utime(p, (new_mtime, new_mtime))
    warning = file_state.check_stale(p, task_id="me")
    assert warning is not None
    assert "modified" in warning.lower()


def test_partial_read_warns_on_overwrite(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("orig\n")
    file_state.record_read(p, task_id="me", partial=True)
    warning = file_state.check_stale(p, task_id="me")
    assert warning is not None
    assert "partial" in warning.lower() or "pagination" in warning.lower()


# ──────────────────────────── note_write semantics ────────────────────────────


def test_note_write_makes_subsequent_writes_safe(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("v1\n")
    file_state.record_read(p, task_id="t1")
    file_state.note_write(p, task_id="t1")
    # Second write by the same task should not warn — ``note_write``
    # is an implicit fresh read for the writer.
    assert file_state.check_stale(p, task_id="t1") is None


def test_brand_new_file_no_warning(tmp_path):
    p = tmp_path / "newly-created.py"
    # File doesn't exist yet — not stale, just non-existent.
    assert file_state.check_stale(p, task_id="t1") is None


# ──────────────────────────── disabled by env ────────────────────────────


def test_env_disable_short_circuits_all_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_DISABLE_FILE_STATE_GUARD", "1")
    p = tmp_path / "a.py"
    p.write_text("orig\n")
    # All accounting calls become no-ops.
    file_state.record_read(p, task_id="t1")
    file_state.note_write(p, task_id="other")
    assert file_state.check_stale(p, task_id="t1") is None
    assert file_state.writes_since("t1", 0, [str(p)]) == {}


# ──────────────────────────── writes_since helper ────────────────────────────


def test_writes_since_returns_sibling_writes(tmp_path):
    p1 = tmp_path / "a.py"
    p2 = tmp_path / "b.py"
    p1.write_text("a\n")
    p2.write_text("b\n")
    parent_read_ts = time.time()
    time.sleep(0.01)
    file_state.note_write(p1, task_id="child-1")
    file_state.note_write(p2, task_id="child-2")
    out = file_state.writes_since(
        exclude_task_id="parent",
        since_ts=parent_read_ts,
        paths=[str(p1), str(p2)],
    )
    # Both children should appear, each with their respective path.
    assert "child-1" in out
    assert "child-2" in out
    assert str(p1.resolve()) in out["child-1"]
    assert str(p2.resolve()) in out["child-2"]


def test_writes_since_excludes_self(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("a\n")
    parent_ts = time.time()
    time.sleep(0.01)
    file_state.note_write(p, task_id="parent")
    out = file_state.writes_since(
        exclude_task_id="parent",
        since_ts=parent_ts,
        paths=[str(p)],
    )
    assert out == {}


# ──────────────────────────── path resolution ────────────────────────────


def test_path_resolution_canonicalizes(tmp_path):
    """Ensure record_read on a relative-ish path matches check_stale on
    the canonical absolute path."""
    p = tmp_path / "sub" / ".." / "a.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "a.py"
    real.write_text("orig\n")
    file_state.record_read(p, task_id="me")  # weird path
    file_state.note_write(real, task_id="other")  # canonical form
    # Should still detect the conflict.
    warning = file_state.check_stale(p, task_id="me")
    assert warning is not None


# ──────────────────────────── cap protection ────────────────────────────


def test_per_agent_dict_capped(tmp_path, monkeypatch):
    """A long session shouldn't accumulate unbounded read state."""
    monkeypatch.setattr(file_state, "_MAX_PATHS_PER_AGENT", 4)
    for i in range(10):
        p = tmp_path / f"f{i}.py"
        p.write_text("x")
        file_state.record_read(p, task_id="t1")
    reads = file_state.known_reads("t1")
    assert len(reads) <= 4
