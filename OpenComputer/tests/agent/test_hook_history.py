"""B2 — HookHistory ring buffer for `oc hooks list` last-fired column.

Module-level deque keyed by event name, maxlen=128. Records
(event, source_id, ts_utc, ok, summary) per fire. Memory-only,
lost on restart (intentional — debug state, not audit state).
"""

from __future__ import annotations

from opencomputer.agent.hook_history import (
    FireRecord,
    all_events,
    clear_history,
    iter_history,
    record_fire,
)


def setup_function() -> None:
    clear_history()


def test_record_and_iter() -> None:
    record_fire("UserPromptSubmit", "plugin:foo", ok=True, summary="ok")
    out = list(iter_history("UserPromptSubmit"))
    assert len(out) == 1
    rec: FireRecord = out[0]
    assert rec.event == "UserPromptSubmit"
    assert rec.source_id == "plugin:foo"
    assert rec.ok is True
    assert rec.summary == "ok"
    assert rec.ts_utc > 0


def test_per_event_isolation() -> None:
    record_fire("UserPromptSubmit", "p1", ok=True, summary="")
    record_fire("ToolCallEnd", "p2", ok=False, summary="boom")
    a = list(iter_history("UserPromptSubmit"))
    b = list(iter_history("ToolCallEnd"))
    assert len(a) == 1
    assert len(b) == 1
    assert a[0].source_id == "p1"
    assert b[0].source_id == "p2"


def test_ring_buffer_caps_at_128() -> None:
    for i in range(200):
        record_fire("UserPromptSubmit", f"p{i}", ok=True, summary=str(i))
    out = list(iter_history("UserPromptSubmit"))
    assert len(out) == 128
    # Oldest entries dropped — newest preserved.
    assert out[-1].source_id == "p199"


def test_clear_history_empties_all() -> None:
    record_fire("UserPromptSubmit", "p1", ok=True, summary="")
    record_fire("ToolCallEnd", "p2", ok=False, summary="")
    clear_history()
    assert list(iter_history("UserPromptSubmit")) == []
    assert list(iter_history("ToolCallEnd")) == []


def test_record_fire_does_not_raise_on_long_summary() -> None:
    record_fire("UserPromptSubmit", "p1", ok=True, summary="x" * 100_000)
    out = list(iter_history("UserPromptSubmit"))
    assert len(out) == 1


def test_iter_history_unknown_event_returns_empty() -> None:
    assert list(iter_history("NoSuchEvent")) == []


def test_all_events_returns_sorted_event_names() -> None:
    record_fire("UserPromptSubmit", "p1", ok=True, summary="")
    record_fire("ToolCallEnd", "p2", ok=True, summary="")
    record_fire("PreToolUse", "p3", ok=True, summary="")
    events = all_events()
    assert events == sorted(events)
    assert "UserPromptSubmit" in events
    assert "ToolCallEnd" in events
    assert "PreToolUse" in events
