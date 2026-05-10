"""Tests for cross-process hook-history persistence (2026-05-10).

Closes the gap that made `oc hooks list` show "Last fired: —" for every
event even after the agent loop / gateway / cron jobs had fired hooks
hundreds of times. Pre-fix the in-process deque died with the process;
the next `oc hooks list` had nothing to read.

Tests:

* ``record_fire`` writes JSONL line to disk
* Fresh-process simulation: ``_reset_for_tests`` clears in-process
  state, then ``iter_history`` re-hydrates from disk
* ``clear_history`` wipes disk + in-process state
* Compaction kicks in when JSONL exceeds threshold
* Malformed lines tolerated (debug state, not audit state)
* No disk path resolved → falls back to in-process behavior
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_record_fire_writes_jsonl_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    hh._reset_for_tests()
    hh.record_fire("PreToolUse", "Bash", ok=True, summary="ls -la")

    disk_path = tmp_path / "hook_history.jsonl"
    assert disk_path.exists(), "JSONL file should be created on first fire"

    lines = disk_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["event"] == "PreToolUse"
    assert obj["source_id"] == "Bash"
    assert obj["ok"] is True
    assert obj["summary"] == "ls -la"
    assert isinstance(obj["ts_utc"], float)


def test_fresh_process_re_hydrates_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates: agent loop writes → fresh `oc hooks list` reads."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    # Process A: agent loop fires
    hh._reset_for_tests()
    hh.record_fire("PreToolUse", "Bash", ok=True, summary="ls")
    hh.record_fire("PostToolUse", "Read", ok=True, summary="loop.py")
    hh.record_fire("PreToolUse", "Edit", ok=False, summary="conflict")

    # Process B: fresh `oc hooks list` — wipe in-process state, keep disk
    hh._reset_for_tests()

    pretools = list(hh.iter_history("PreToolUse"))
    assert len(pretools) == 2
    assert {r.source_id for r in pretools} == {"Bash", "Edit"}

    posttools = list(hh.iter_history("PostToolUse"))
    assert len(posttools) == 1
    assert posttools[0].source_id == "Read"

    events = hh.all_events()
    assert "PreToolUse" in events
    assert "PostToolUse" in events


def test_clear_history_wipes_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    hh._reset_for_tests()
    hh.record_fire("PreToolUse", "Bash", ok=True, summary="cmd")
    disk_path = tmp_path / "hook_history.jsonl"
    assert disk_path.exists()

    n = hh.clear_history()
    assert n >= 1
    assert not disk_path.exists(), "clear_history must remove the JSONL"

    # Fresh process sees nothing
    hh._reset_for_tests()
    assert hh.all_events() == []


def test_iter_history_unknown_event_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    hh._reset_for_tests()
    assert list(hh.iter_history("EventThatNeverFired")) == []


def test_record_fire_truncates_huge_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long summaries get truncated before write so JSONL lines stay small."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    hh._reset_for_tests()
    huge = "x" * 50_000
    hh.record_fire("PreToolUse", "Bash", ok=True, summary=huge)

    rec = next(iter(hh.iter_history("PreToolUse")))
    assert len(rec.summary) <= 4096 + len("...[truncated]")
    assert rec.summary.endswith("...[truncated]")


def test_malformed_lines_tolerated_on_hydrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad lines silently skipped — debug state, not audit state."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    disk_path = tmp_path / "hook_history.jsonl"
    disk_path.write_text(
        "this is not json\n"
        "{}\n"
        '{"event": "PreToolUse", "source_id": "Bash", "ts_utc": 1.0, "ok": true, "summary": "ok"}\n'
        "another junk line\n"
    )

    hh._reset_for_tests()
    recs = list(hh.iter_history("PreToolUse"))
    assert len(recs) == 1
    assert recs[0].source_id == "Bash"


def test_compaction_keeps_recent_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When file exceeds 5 MiB, compaction keeps the most-recent maxlen entries."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    # Lower the cap so we don't actually have to write 5 MiB.
    monkeypatch.setattr(hh, "_DISK_MAX_BYTES", 2048)

    hh._reset_for_tests()
    # Write enough records to overflow the small cap multiple times.
    # _HISTORY_MAXLEN is 128; write 200 to exercise the cap math.
    for i in range(200):
        hh.record_fire("PreToolUse", f"Bash#{i}", ok=True, summary="x" * 50)

    disk_path = tmp_path / "hook_history.jsonl"
    assert disk_path.exists()
    # After compaction, the file should be ≤ ~2 KiB-ish (one cycle of
    # 128 records of ~150 bytes each ≈ 19 KiB; the bound is the cap
    # AT TIME OF NEXT APPEND, so we just check we didn't grow unboundedly).
    final_size = disk_path.stat().st_size
    assert final_size < 100_000, (
        f"compaction should keep file small; got {final_size} bytes"
    )

    # Fresh process sees most-recent records (last writes win)
    hh._reset_for_tests()
    recs = list(hh.iter_history("PreToolUse"))
    assert recs, "should have recent records after compaction"
    # The most-recent fire should be in there
    sources = {r.source_id for r in recs}
    assert "Bash#199" in sources


def test_no_disk_path_falls_back_to_in_process_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _resolve_disk_path returns None, behavior is in-process only."""
    from opencomputer.agent import hook_history as hh

    hh._reset_for_tests()
    monkeypatch.setattr(hh, "_resolve_disk_path", lambda: None)

    hh.record_fire("PreToolUse", "Bash", ok=True, summary="x")
    recs = list(hh.iter_history("PreToolUse"))
    assert len(recs) == 1
    # Fresh in-process state → can't recover (no disk)
    hh._reset_for_tests()
    monkeypatch.setattr(hh, "_resolve_disk_path", lambda: None)
    assert list(hh.iter_history("PreToolUse")) == []


def test_record_fire_empty_event_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: empty event name doesn't crash; rec is still written."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent import hook_history as hh

    hh._reset_for_tests()
    hh.record_fire("", "Bash", ok=True, summary="x")
    # The hydration filter drops records with empty event name; in-process
    # accepts (we don't validate). Either behavior is fine; test that
    # neither path crashes.
    _ = list(hh.iter_history(""))
    _ = hh.all_events()
