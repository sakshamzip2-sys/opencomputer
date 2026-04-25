"""Tests for the three Phase 3.B :class:`MotifExtractor` implementations."""

from __future__ import annotations

import datetime as _dt

import pytest

from opencomputer.inference.extractors import (
    ImplicitGoalExtractor,
    TemporalMotifExtractor,
    TransitionChainExtractor,
)
from plugin_sdk.ingestion import (
    SignalEvent,
    ToolCallEvent,
    WebObservationEvent,
)


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Bus swap+restore — preserves the cross-file singleton invariant."""
    from opencomputer.ingestion import bus as bus_module
    from opencomputer.ingestion.bus import reset_default_bus

    saved = bus_module.default_bus
    reset_default_bus()
    yield
    bus_module.default_bus = saved


# Helpers ─────────────────────────────────────────────────────────────


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return _dt.datetime(
        year, month, day, hour, minute, 0, tzinfo=_dt.UTC
    ).timestamp()


def _tc(
    *,
    tool_name: str,
    timestamp: float,
    source: str = "agent_loop",
    session_id: str | None = None,
) -> ToolCallEvent:
    return ToolCallEvent(
        tool_name=tool_name,
        timestamp=timestamp,
        source=source,
        session_id=session_id,
    )


# ─── Temporal ─────────────────────────────────────────────────────────


def test_temporal_motif_finds_recurring_pattern() -> None:
    """4 Read calls at Monday 09:xx UTC → one temporal motif."""
    base = _ts(2026, 1, 5, 9)  # 2026-01-05 = Monday
    events = [_tc(tool_name="Read", timestamp=base + i * 60) for i in range(4)]
    ex = TemporalMotifExtractor()
    motifs = ex.extract(events)
    assert len(motifs) == 1
    m = motifs[0]
    assert m.kind == "temporal"
    assert m.support == 4
    assert m.payload["hour"] == 9
    assert m.payload["day_of_week"] == 0  # Monday
    assert m.payload["label"] == "Read"
    assert m.payload["count"] == 4
    assert "Monday" in m.summary
    assert "09:00" in m.summary
    assert pytest.approx(m.confidence, abs=1e-6) == 0.4
    # support-fixture: 4 hits → confidence 0.4 (4/10)


def test_temporal_motif_skips_sparse_buckets() -> None:
    """Buckets with fewer than 3 events do not emit motifs."""
    base = _ts(2026, 1, 5, 9)
    # 2 events in the same bucket — below the 3-event threshold.
    events = [_tc(tool_name="Read", timestamp=base + i * 60) for i in range(2)]
    ex = TemporalMotifExtractor()
    assert ex.extract(events) == []


def test_temporal_motif_separates_different_tools() -> None:
    """Two distinct tools in the same hour produce two separate motifs."""
    base = _ts(2026, 1, 5, 9)
    events: list[SignalEvent] = []
    for i in range(3):
        events.append(_tc(tool_name="Read", timestamp=base + i * 60))
    for i in range(3):
        events.append(_tc(tool_name="Bash", timestamp=base + 200 + i * 60))
    ex = TemporalMotifExtractor()
    motifs = ex.extract(events)
    labels = sorted(m.payload["label"] for m in motifs)
    assert labels == ["Bash", "Read"]


# ─── Transition ───────────────────────────────────────────────────────


def test_transition_chain_finds_repeat_pair() -> None:
    """Read→Bash seen 3 times → one transition motif with prob 1.0."""
    base = _ts(2026, 1, 5, 10)
    events: list[SignalEvent] = []
    # Three Read→Bash pairs, each pair within 30 seconds, separated by
    # 1000 seconds (so the next "Read" doesn't form a transition with
    # the prior "Bash").
    for round_idx in range(3):
        events.append(_tc(tool_name="Read", timestamp=base + round_idx * 1000))
        events.append(
            _tc(tool_name="Bash", timestamp=base + round_idx * 1000 + 30)
        )
    ex = TransitionChainExtractor()
    motifs = ex.extract(events)
    # 3 Read→Bash transitions; the Bash→Read between rounds is ruled
    # out by the 1000-second gap (> 300s window).
    rb = [
        m
        for m in motifs
        if m.payload["prev"] == "tool_call/Read"
        and m.payload["curr"] == "tool_call/Bash"
    ]
    assert len(rb) == 1
    m = rb[0]
    assert m.support == 3
    assert m.payload["count"] == 3
    assert pytest.approx(m.payload["probability"], abs=1e-6) == 1.0
    assert "After tool_call/Read" in m.summary


def test_transition_chain_respects_5min_window() -> None:
    """Adjacent events spaced > 300 seconds apart do NOT form transitions."""
    base = _ts(2026, 1, 5, 10)
    # Pair of Read→Bash separated by 600s (10 minutes) — no transition.
    events = [
        _tc(tool_name="Read", timestamp=base),
        _tc(tool_name="Bash", timestamp=base + 600),
        _tc(tool_name="Read", timestamp=base + 1200),
        _tc(tool_name="Bash", timestamp=base + 1800),
    ]
    ex = TransitionChainExtractor()
    motifs = ex.extract(events)
    # Every gap is 600s, so nothing forms a valid transition.
    assert motifs == []


def test_transition_chain_ignores_single_pair() -> None:
    """A transition seen only once is below the count >= 2 threshold."""
    base = _ts(2026, 1, 5, 10)
    events = [
        _tc(tool_name="Read", timestamp=base),
        _tc(tool_name="Bash", timestamp=base + 30),
    ]
    ex = TransitionChainExtractor()
    assert ex.extract(events) == []


# ─── Implicit goal ────────────────────────────────────────────────────


def test_implicit_goal_skips_sparse_sessions() -> None:
    """Sessions with fewer than 3 distinct tool names yield no motif."""
    base = _ts(2026, 1, 5, 11)
    # 5 events but only 2 distinct tools — should be skipped.
    events = [
        _tc(tool_name="Read", timestamp=base, session_id="s-sparse"),
        _tc(tool_name="Read", timestamp=base + 60, session_id="s-sparse"),
        _tc(tool_name="Bash", timestamp=base + 120, session_id="s-sparse"),
        _tc(tool_name="Bash", timestamp=base + 180, session_id="s-sparse"),
        _tc(tool_name="Read", timestamp=base + 240, session_id="s-sparse"),
    ]
    ex = ImplicitGoalExtractor()
    assert ex.extract(events) == []


def test_implicit_goal_summarizes_session() -> None:
    """Session with 4+ distinct tools yields one motif with top_5 list."""
    base = _ts(2026, 1, 5, 11)
    sid = "session-abc12345-rest"
    events = [
        _tc(tool_name="Read", timestamp=base, session_id=sid),
        _tc(tool_name="Bash", timestamp=base + 60, session_id=sid),
        _tc(tool_name="Grep", timestamp=base + 120, session_id=sid),
        _tc(tool_name="Edit", timestamp=base + 180, session_id=sid),
        _tc(tool_name="Read", timestamp=base + 240, session_id=sid),
        _tc(tool_name="Read", timestamp=base + 300, session_id=sid),
    ]
    ex = ImplicitGoalExtractor()
    motifs = ex.extract(events)
    assert len(motifs) == 1
    m = motifs[0]
    assert m.kind == "implicit_goal"
    assert m.session_id == sid
    assert m.payload["session_id"] == sid
    assert m.payload["n_events"] == 6
    assert m.payload["n_distinct_tools"] == 4
    # Read appears 3 times — must be first in top_tools.
    assert m.payload["top_tools"][0] == "Read"
    assert set(m.payload["top_tools"]) == {"Read", "Bash", "Grep", "Edit"}
    assert sid[:8] in m.summary


def test_implicit_goal_uses_session_id_grouping() -> None:
    """Two sessions with their own 3+ distinct tools yield two motifs."""
    base = _ts(2026, 1, 5, 11)
    events = [
        _tc(tool_name="Read", timestamp=base, session_id="alpha"),
        _tc(tool_name="Bash", timestamp=base + 60, session_id="alpha"),
        _tc(tool_name="Grep", timestamp=base + 120, session_id="alpha"),
        _tc(tool_name="Edit", timestamp=base + 180, session_id="beta"),
        _tc(tool_name="Glob", timestamp=base + 240, session_id="beta"),
        _tc(tool_name="Write", timestamp=base + 300, session_id="beta"),
    ]
    ex = ImplicitGoalExtractor()
    motifs = ex.extract(events)
    sessions = {m.session_id for m in motifs}
    assert sessions == {"alpha", "beta"}


def test_implicit_goal_ignores_non_toolcall_events() -> None:
    """WebObservationEvents are skipped (no tool_name to summarise)."""
    base = _ts(2026, 1, 5, 11)
    events: list[SignalEvent] = [
        WebObservationEvent(
            url="https://example.com",
            timestamp=base + i,
            session_id="s1",
        )
        for i in range(5)
    ]
    ex = ImplicitGoalExtractor()
    assert ex.extract(events) == []
