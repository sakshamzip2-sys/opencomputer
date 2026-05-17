"""Tests for the per-profile ``life_event_state.json`` store.

Mirrors the profile-isolation pattern used by ``tests/test_cli_awareness.py``:
``OPENCOMPUTER_HOME`` is monkey-patched to a ``tmp_path`` so the real user
profile is never touched. ``state.py`` resolves the profile home through
``opencomputer.agent.config._home`` (the canonical core resolver), which
honors ``OPENCOMPUTER_HOME``.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from opencomputer.awareness.life_events import state


def test_load_state_missing_file_returns_empty(tmp_path, monkeypatch):
    """No state file on disk → an empty dict, never a raise."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert state.load_state() == {}


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    """``save_state`` followed by ``load_state`` returns the same payload."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    payload = {
        "burnout": {
            "firing_ts": 123.0,
            "cron_id": "cron-abc",
            "surfaced": True,
            "verdict_pending": True,
        }
    }
    state.save_state(payload)
    assert state.load_state() == payload


def test_save_state_writes_to_profile_home(tmp_path, monkeypatch):
    """The file lands at ``<profile-home>/life_event_state.json``."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.save_state({"travel": {"firing_ts": 1.0}})
    state_path = tmp_path / "life_event_state.json"
    assert state_path.exists()
    assert json.loads(state_path.read_text()) == {"travel": {"firing_ts": 1.0}}


def test_corrupt_file_returns_empty(tmp_path, monkeypatch, caplog):
    """A non-JSON / unparseable file → an empty dict, never a raise, and a WARNING."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "life_event_state.json").write_text("{not valid json at all")
    with caplog.at_level(logging.WARNING):
        result = state.load_state()
    assert result == {}
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "expected a WARNING log record for corrupt file"
    )
    assert "life_event_state.json" in caplog.text


def test_non_dict_file_returns_empty(tmp_path, monkeypatch, caplog):
    """Valid JSON that isn't an object (e.g. a list) → an empty dict and a WARNING."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "life_event_state.json").write_text("[1, 2, 3]")
    with caplog.at_level(logging.WARNING):
        result = state.load_state()
    assert result == {}
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "expected a WARNING log record for non-dict file"
    )
    assert "list" in caplog.text


def test_mark_surfaced_records_full_entry(tmp_path, monkeypatch):
    """``mark_surfaced`` records firing_ts/cron_id/surfaced/verdict_pending."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    before = time.time()
    state.mark_surfaced("exam_prep", "cron-42")
    after = time.time()

    loaded = state.load_state()
    assert "exam_prep" in loaded
    entry = loaded["exam_prep"]
    assert entry["cron_id"] == "cron-42"
    assert entry["surfaced"] is True
    assert entry["verdict_pending"] is True
    assert before <= entry["firing_ts"] <= after

    # verdict_pending_patterns() must include a freshly-surfaced pattern.
    assert "exam_prep" in state.verdict_pending_patterns()


def test_mark_surfaced_overwrites_existing_entry(tmp_path, monkeypatch):
    """A second ``mark_surfaced`` replaces the prior entry (new cron_id)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.mark_surfaced("burnout", "cron-old")
    state.mark_surfaced("burnout", "cron-new")
    entry = state.load_state()["burnout"]
    assert entry["cron_id"] == "cron-new"
    assert entry["verdict_pending"] is True


def test_clear_verdict_pending_keeps_entry_and_cron(tmp_path, monkeypatch):
    """``clear_verdict_pending`` flips verdict_pending off but keeps cron_id."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.mark_surfaced("job_change", "cron-7")
    state.clear_verdict_pending("job_change")

    # No longer verdict-pending …
    assert "job_change" not in state.verdict_pending_patterns()
    # … but the entry survives, WITH its cron_id.
    loaded = state.load_state()
    assert "job_change" in loaded
    assert loaded["job_change"]["cron_id"] == "cron-7"
    assert loaded["job_change"]["verdict_pending"] is False
    assert loaded["job_change"]["surfaced"] is True


def test_clear_verdict_pending_missing_pattern_is_noop(tmp_path, monkeypatch):
    """Clearing verdict-pending on an unknown pattern raises nothing."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.clear_verdict_pending("never_surfaced")  # must not raise
    assert state.load_state() == {}


def test_clear_removes_entry_entirely(tmp_path, monkeypatch):
    """``clear`` drops the whole entry — pattern_id and cron_id both gone."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.mark_surfaced("health_event", "cron-9")
    assert "health_event" in state.load_state()

    state.clear("health_event")
    assert state.load_state() == {}
    assert "health_event" not in state.verdict_pending_patterns()


def test_clear_missing_pattern_is_noop(tmp_path, monkeypatch):
    """Clearing an unknown pattern raises nothing and changes nothing."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.mark_surfaced("travel", "cron-1")
    state.clear("never_surfaced")  # must not raise
    assert "travel" in state.load_state()


def test_verdict_pending_patterns_filters_by_flag(tmp_path, monkeypatch):
    """Only patterns with a truthy verdict_pending are returned."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.mark_surfaced("burnout", "cron-a")
    state.mark_surfaced("travel", "cron-b")
    state.clear_verdict_pending("travel")

    pending = state.verdict_pending_patterns()
    assert "burnout" in pending
    assert "travel" not in pending


def test_verdict_pending_patterns_empty_when_no_state(tmp_path, monkeypatch):
    """No state file → an empty list, never a raise."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert state.verdict_pending_patterns() == []


def test_concurrent_mark_surfaced_keeps_every_entry(tmp_path, monkeypatch):
    """N threads each ``mark_surfaced`` a distinct pattern — none lost.

    The mutators do a load → mutate → save read-modify-write. Without a
    lock around that whole sequence, two threads can both load the same
    baseline and the slower writer clobbers the faster one's entry.

    To make that race observable deterministically, ``save_state`` is
    widened with a brief sleep BETWEEN load and save so the threads
    interleave inside the unprotected window. With the file lock in place
    only one thread is ever inside ``load → mutate → save`` at a time, so
    all N entries survive; without it, entries are lost.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    real_save = state.save_state

    def slow_save(payload: dict) -> None:
        # Widen the load→save race window. The lock (if present) is held
        # across this call, so the sleep does not itself cause a deadlock.
        time.sleep(0.05)
        real_save(payload)

    monkeypatch.setattr(state, "save_state", slow_save)

    n_threads = 8
    pattern_ids = [f"pattern_{i}" for i in range(n_threads)]

    def worker(pattern_id: str) -> None:
        state.mark_surfaced(pattern_id, f"cron-{pattern_id}")

    threads = [
        threading.Thread(target=worker, args=(pid,)) for pid in pattern_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = state.load_state()
    missing = [pid for pid in pattern_ids if pid not in loaded]
    assert not missing, (
        f"lost {len(missing)}/{n_threads} concurrent mark_surfaced updates: "
        f"{missing}"
    )
    assert len(loaded) == n_threads
