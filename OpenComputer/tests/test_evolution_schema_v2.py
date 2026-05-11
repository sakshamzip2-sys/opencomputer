"""Schema v2 — cross-process rolling-window persistence + tune events.

Covers:

1. The rolling window survives orchestrator restart (CLI mode where
   each invocation creates a fresh orchestrator).
2. ``load_recent_decisions`` is forward-compat with v1 files (missing
   ``recent_decisions`` → empty list).
3. ``EvolutionTuningChangedEvent`` is published on every recompute,
   with ``changed`` set accurately based on diff vs prior state.
4. The persisted window is capped at ``_PERSISTED_DECISIONS_CAP``.
5. Reset re-emits a tuning-changed event so dashboards refresh.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from opencomputer.agent.evolution_orchestrator import (
    DEFAULT_TUNING,
    SCHEMA_VERSION,
    EvolutionOrchestrator,
    load_recent_decisions,
    load_tuning,
)
from plugin_sdk.ingestion import EvolutionTuningChangedEvent


class _StubBus:
    def __init__(self) -> None:
        self.subs: dict[str, list] = {}
        self.published: list = []

    def subscribe(self, event_type, handler):  # noqa: ANN001
        self.subs.setdefault(event_type, []).append(handler)

        class _Sub:
            def unsubscribe(_):  # noqa: N805, ARG002
                self.subs[event_type].remove(handler)

        return _Sub()

    def publish(self, event):  # noqa: ANN001
        self.published.append(event)


class _Evt:
    """Duck-typed SkillReviewDecisionEvent for direct handler calls."""

    def __init__(self, *, decision, name="auto", session="s", trace="t", conf=80):
        self.decision = decision
        self.skill_name = name
        self.origin_session_id = session
        self.trace_id = trace
        self.confidence_at_proposal = conf
        self.timestamp = time.time()


# ─── window survives orchestrator restart ────────────────────────────


def test_window_survives_orchestrator_restart(tmp_path: Path):
    """Two orchestrator instances against the same profile_home share
    the rolling window via the persisted v2 file."""
    bus1 = _StubBus()
    orch1 = EvolutionOrchestrator(bus=bus1, profile_home=tmp_path)
    orch1.start()
    for _ in range(5):
        orch1._on_decision(_Evt(decision="accepted"))
    assert len(orch1.window_snapshot()) == 5
    orch1.stop()

    # Fresh orchestrator — like a new CLI process. Must hydrate.
    bus2 = _StubBus()
    orch2 = EvolutionOrchestrator(bus=bus2, profile_home=tmp_path)
    orch2.start()
    hydrated = orch2.window_snapshot()
    assert len(hydrated) == 5, (
        "v2 schema should persist + hydrate the window across orchestrator instances"
    )
    assert all(r.decision == "accepted" for r in hydrated)
    orch2.stop()


def test_cross_process_total_decisions_carries(tmp_path: Path):
    """The total_decisions_observed counter survives restart so the
    modulo-N auto-tune trigger fires correctly across CLI invocations."""
    bus1 = _StubBus()
    orch1 = EvolutionOrchestrator(bus=bus1, profile_home=tmp_path)
    orch1.start()
    for _ in range(7):
        orch1._on_decision(_Evt(decision="rejected"))
    orch1.stop()

    persisted = load_tuning(tmp_path)
    assert persisted.decisions_observed == 7

    bus2 = _StubBus()
    orch2 = EvolutionOrchestrator(bus=bus2, profile_home=tmp_path)
    orch2.start()
    # Add 3 more — total now 10 — should opportunistic-tune.
    for _ in range(3):
        orch2._on_decision(_Evt(decision="rejected"))
    after = load_tuning(tmp_path)
    # 10/10 rejected → accept_rate 0% → tighten confidence by 5.
    assert after.confidence_threshold == DEFAULT_TUNING.confidence_threshold + 5
    assert after.decisions_observed == 10
    orch2.stop()


def test_v1_file_hydrates_as_empty_window(tmp_path: Path):
    """A v1 file (no recent_decisions field) is forward-compat: tunables
    parse, window hydrates to empty."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "confidence_threshold": 80,
                "dreaming_v2_score_threshold": 0.7,
                "dreaming_v2_min_recall": 3,
                "decisions_observed": 25,
                "last_recompute_ts": 1000.0,
            }
        )
    )
    assert load_tuning(tmp_path).confidence_threshold == 80
    assert load_recent_decisions(tmp_path) == [], (
        "v1 file has no recent_decisions → empty window"
    )

    # New orchestrator hydrates total=25 but window=empty.
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    assert orch._total_decisions_observed == 25
    assert orch.window_snapshot() == []
    orch.stop()


def test_persisted_window_capped(tmp_path: Path):
    """Window persists at most _PERSISTED_DECISIONS_CAP entries."""
    from opencomputer.agent.evolution_orchestrator import _PERSISTED_DECISIONS_CAP

    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    # Push 30 decisions.
    for i in range(30):
        orch._on_decision(_Evt(decision="deferred", name=f"x-{i}"))
    orch.stop()

    persisted = load_recent_decisions(tmp_path)
    assert len(persisted) == _PERSISTED_DECISIONS_CAP
    # FIFO: last N retained.
    assert persisted[-1].skill_name == "x-29"
    assert persisted[0].skill_name == f"x-{30 - _PERSISTED_DECISIONS_CAP}"


# ─── EvolutionTuningChangedEvent ─────────────────────────────────────


def test_tuning_changed_event_published_on_recompute(tmp_path: Path):
    """Every successful recompute publishes the event."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    # Trigger a real change: 10 rejections.
    for _ in range(10):
        orch._on_decision(_Evt(decision="rejected"))
    orch.stop()

    events = [e for e in bus.published if isinstance(e, EvolutionTuningChangedEvent)]
    assert len(events) >= 1
    final = events[-1]
    assert final.confidence_threshold == DEFAULT_TUNING.confidence_threshold + 5
    assert final.changed is True
    assert final.decisions_observed == 10


def test_tuning_changed_event_no_change_flag(tmp_path: Path):
    """A no-op recompute publishes with ``changed=False``."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    # 10 deferred decisions — non-counted, so recompute math returns
    # the same tuning (deferred excluded from accept-rate denominator).
    # We have to drive a recompute manually because the auto-tune
    # trigger fires on decision count, not non-deferred count.
    for _ in range(10):
        orch._on_decision(_Evt(decision="deferred"))
    # The auto-tune already fired at decision 10. Both that AND any
    # subsequent manual recompute should publish with changed=False
    # since 10 deferred → no math change.
    orch.recompute_tuning()
    orch.stop()

    events = [e for e in bus.published if isinstance(e, EvolutionTuningChangedEvent)]
    assert len(events) >= 1
    assert all(e.changed is False for e in events), (
        "deferred-only decisions should not change tuning"
    )


def test_reset_publishes_tuning_changed_event(tmp_path: Path):
    """``reset()`` publishes a tuning-changed event so dashboards refresh."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()

    # Drive tuning off defaults first.
    for _ in range(10):
        orch._on_decision(_Evt(decision="rejected"))
    assert load_tuning(tmp_path).confidence_threshold != DEFAULT_TUNING.confidence_threshold

    bus.published.clear()
    orch.reset()
    orch.stop()

    events = [e for e in bus.published if isinstance(e, EvolutionTuningChangedEvent)]
    assert len(events) == 1
    assert events[0].changed is True
    assert events[0].confidence_threshold == DEFAULT_TUNING.confidence_threshold
    assert events[0].decisions_observed == 0


def test_schema_version_persisted_correctly(tmp_path: Path):
    """The current SCHEMA_VERSION is what gets written to disk."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    orch._on_decision(_Evt(decision="accepted"))
    orch.stop()

    raw = json.loads(
        (tmp_path / "skills" / "evolution_tuning.json").read_text()
    )
    assert raw["schema_version"] == SCHEMA_VERSION
    assert "recent_decisions" in raw
    assert isinstance(raw["recent_decisions"], list)
    assert len(raw["recent_decisions"]) == 1
