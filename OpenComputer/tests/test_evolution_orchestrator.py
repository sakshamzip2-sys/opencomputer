"""Tests for ``opencomputer.agent.evolution_orchestrator``.

Covers:
* Pure tuning math (``compute_new_tuning``) across accept-rate
  regimes (low / dead-band / high) and undersized windows.
* Persistence round-trip via ``load_tuning``.
* End-to-end orchestrator: subscribe → receive decisions → tune.
* Reset + error-tolerant load paths.

The orchestrator subscribes to the F2 bus. We construct a tiny
in-memory bus stand-in to drive ``_on_decision`` directly rather than
spinning up the real ``opencomputer.ingestion.bus`` (which has its
own tests and would couple us to its internals).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from opencomputer.agent.evolution_orchestrator import (
    DEFAULT_TUNING,
    SCHEMA_VERSION,
    DecisionRecord,
    EvolutionOrchestrator,
    EvolutionTuning,
    compute_new_tuning,
    load_tuning,
)


class _StubBus:
    """Captures subscriptions for inspection; never fans out."""

    def __init__(self) -> None:
        self.subs: dict[str, list] = {}

    def subscribe(self, event_type: str, handler):  # noqa: ANN001
        self.subs.setdefault(event_type, []).append(handler)

        class _Handle:
            def unsubscribe(_self):  # noqa: N805, ARG002
                self.subs[event_type].remove(handler)

        return _Handle()


class _DecisionEvt:
    """Duck-typed SkillReviewDecisionEvent — orchestrator only reads attrs."""

    def __init__(
        self,
        *,
        decision: str,
        skill_name: str = "auto-test",
        origin_session_id: str = "s1",
        trace_id: str = "",
        confidence: int = 75,
        timestamp: float | None = None,
    ) -> None:
        self.decision = decision
        self.skill_name = skill_name
        self.origin_session_id = origin_session_id
        self.trace_id = trace_id
        self.confidence_at_proposal = confidence
        self.timestamp = timestamp if timestamp is not None else time.time()


# ─── pure tuning math ────────────────────────────────────────────────


def test_compute_new_tuning_below_min_decisions():
    """Fewer than ``_MIN_DECISIONS_TO_TUNE`` (10) → no threshold change."""
    window = [
        DecisionRecord(timestamp=0, skill_name="x", decision="rejected")
        for _ in range(5)
    ]
    new = compute_new_tuning(
        window=window, current=DEFAULT_TUNING, total_decisions=5
    )
    assert new.confidence_threshold == DEFAULT_TUNING.confidence_threshold
    assert new.dreaming_v2_score_threshold == DEFAULT_TUNING.dreaming_v2_score_threshold


def test_compute_new_tuning_low_accept_rate_tightens():
    """<30% accept → +5 confidence, +0.05 score, +1 recall."""
    window = [
        DecisionRecord(timestamp=0, skill_name="x", decision="rejected")
        for _ in range(9)
    ] + [DecisionRecord(timestamp=0, skill_name="x", decision="accepted")]
    new = compute_new_tuning(
        window=window, current=DEFAULT_TUNING, total_decisions=10
    )
    assert new.confidence_threshold == DEFAULT_TUNING.confidence_threshold + 5
    assert (
        new.dreaming_v2_score_threshold
        == pytest.approx(DEFAULT_TUNING.dreaming_v2_score_threshold + 0.05)
    )
    assert (
        new.dreaming_v2_min_recall
        == DEFAULT_TUNING.dreaming_v2_min_recall + 1
    )


def test_compute_new_tuning_high_accept_rate_loosens():
    """>80% accept → −5 confidence, −0.05 score, −1 recall."""
    window = [
        DecisionRecord(timestamp=0, skill_name="x", decision="accepted")
        for _ in range(9)
    ] + [DecisionRecord(timestamp=0, skill_name="x", decision="rejected")]
    new = compute_new_tuning(
        window=window, current=DEFAULT_TUNING, total_decisions=10
    )
    assert new.confidence_threshold == DEFAULT_TUNING.confidence_threshold - 5
    assert (
        new.dreaming_v2_score_threshold
        == pytest.approx(DEFAULT_TUNING.dreaming_v2_score_threshold - 0.05)
    )
    assert (
        new.dreaming_v2_min_recall
        == DEFAULT_TUNING.dreaming_v2_min_recall - 1
    )


def test_compute_new_tuning_dead_band_no_change():
    """Accept rate inside [0.30, 0.80] → no tuning change."""
    # 5/10 = 50% — squarely in dead band.
    window = (
        [DecisionRecord(timestamp=0, skill_name="x", decision="accepted")]
        * 5
        + [DecisionRecord(timestamp=0, skill_name="x", decision="rejected")]
        * 5
    )
    new = compute_new_tuning(
        window=window, current=DEFAULT_TUNING, total_decisions=10
    )
    assert new.confidence_threshold == DEFAULT_TUNING.confidence_threshold


def test_compute_new_tuning_edited_counts_half():
    """``"edited"`` decision counts as 0.5 in the accept-rate numerator.

    10 edits → 5.0/10 = 0.5 → dead band → no change.
    """
    window = [
        DecisionRecord(timestamp=0, skill_name="x", decision="edited")
        for _ in range(10)
    ]
    new = compute_new_tuning(
        window=window, current=DEFAULT_TUNING, total_decisions=10
    )
    assert new.confidence_threshold == DEFAULT_TUNING.confidence_threshold


def test_compute_new_tuning_deferred_does_not_count():
    """``"deferred"`` is excluded from the rate denominator."""
    # 10 rejected + 10 deferred = 0% over 10 counted → tighten.
    window = (
        [DecisionRecord(timestamp=0, skill_name="x", decision="rejected")]
        * 10
        + [DecisionRecord(timestamp=0, skill_name="x", decision="deferred")]
        * 10
    )
    new = compute_new_tuning(
        window=window, current=DEFAULT_TUNING, total_decisions=20
    )
    assert new.confidence_threshold == DEFAULT_TUNING.confidence_threshold + 5


def test_compute_new_tuning_clamps_at_max():
    """Repeated tightening cannot push thresholds beyond their caps."""
    pinned = EvolutionTuning(
        confidence_threshold=95,  # already at max
        dreaming_v2_score_threshold=0.90,
        dreaming_v2_min_recall=5,
    )
    window = [
        DecisionRecord(timestamp=0, skill_name="x", decision="rejected")
        for _ in range(10)
    ]
    new = compute_new_tuning(
        window=window, current=pinned, total_decisions=10
    )
    assert new.confidence_threshold == 95
    assert new.dreaming_v2_score_threshold == pytest.approx(0.90)
    assert new.dreaming_v2_min_recall == 5


def test_compute_new_tuning_clamps_at_min():
    """Repeated loosening cannot drive thresholds below their floors."""
    pinned = EvolutionTuning(
        confidence_threshold=50,
        dreaming_v2_score_threshold=0.40,
        dreaming_v2_min_recall=1,
    )
    window = [
        DecisionRecord(timestamp=0, skill_name="x", decision="accepted")
        for _ in range(10)
    ]
    new = compute_new_tuning(
        window=window, current=pinned, total_decisions=10
    )
    assert new.confidence_threshold == 50
    assert new.dreaming_v2_score_threshold == pytest.approx(0.40)
    assert new.dreaming_v2_min_recall == 1


# ─── persistence ─────────────────────────────────────────────────────


def test_load_tuning_missing_file_returns_defaults(tmp_path: Path):
    """No tuning file → defaults."""
    assert load_tuning(tmp_path) == DEFAULT_TUNING


def test_load_tuning_malformed_json_returns_defaults(tmp_path: Path):
    """Bad JSON → defaults + warning logged (not crash)."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "evolution_tuning.json").write_text("not valid json")
    assert load_tuning(tmp_path) == DEFAULT_TUNING


def test_load_tuning_wrong_schema_version_returns_defaults(tmp_path: Path):
    """Mismatched schema_version → defaults."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": 999,
                "confidence_threshold": 80,
            }
        )
    )
    assert load_tuning(tmp_path) == DEFAULT_TUNING


def test_load_tuning_clamps_out_of_range_values(tmp_path: Path):
    """Out-of-range persisted values are clamped on read."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "confidence_threshold": 9999,  # > max
                "dreaming_v2_score_threshold": -1.0,  # < min
                "dreaming_v2_min_recall": 100,  # > max
            }
        )
    )
    tuning = load_tuning(tmp_path)
    assert tuning.confidence_threshold == 95  # _CONFIDENCE_MAX
    assert tuning.dreaming_v2_score_threshold == 0.40  # _DREAM_SCORE_MIN
    assert tuning.dreaming_v2_min_recall == 5  # _DREAM_RECALL_MAX


# ─── orchestrator integration ────────────────────────────────────────


def test_orchestrator_subscribes_to_both_event_types(tmp_path: Path):
    """``start()`` subscribes to ``skill_review_decision`` + ``turn_completed``."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    assert "skill_review_decision" in bus.subs
    assert "turn_completed" in bus.subs
    orch.stop()


def test_orchestrator_decision_handler_records(tmp_path: Path):
    """Each decision lands in the rolling window."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()

    orch._on_decision(_DecisionEvt(decision="accepted"))
    orch._on_decision(_DecisionEvt(decision="rejected"))

    snapshot = orch.window_snapshot()
    assert len(snapshot) == 2
    assert {r.decision for r in snapshot} == {"accepted", "rejected"}
    orch.stop()


def test_orchestrator_opportunistic_tune_after_min_decisions(tmp_path: Path):
    """After ``_MIN_DECISIONS_TO_TUNE`` decisions, a tune runs automatically."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()

    # 10 rejected decisions → tighten on the 10th.
    for _ in range(10):
        orch._on_decision(_DecisionEvt(decision="rejected"))

    persisted = load_tuning(tmp_path)
    assert persisted.confidence_threshold == DEFAULT_TUNING.confidence_threshold + 5
    assert persisted.decisions_observed == 10
    orch.stop()


def test_orchestrator_manual_recompute(tmp_path: Path):
    """``recompute_tuning`` runs the math + persists, regardless of count.

    Note: feeding exactly 10 decisions triggers an opportunistic
    auto-tune AND we then manually recompute, so the assertion accounts
    for two steps of loosening (10 below + 5 more from manual recompute).
    """
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()

    # Feed exactly 10 accepted (>80% rate → loosen on the 10th).
    for _ in range(10):
        orch._on_decision(_DecisionEvt(decision="accepted"))
    # Auto-tune already fired at decision 10 (70 → 65).
    assert load_tuning(tmp_path).confidence_threshold == 65

    # Manual recompute runs the math again on the same window; another
    # step of loosening: 65 → 60.
    new = orch.recompute_tuning()
    assert new.confidence_threshold == 60
    persisted = load_tuning(tmp_path)
    assert persisted.confidence_threshold == new.confidence_threshold
    orch.stop()


def test_orchestrator_reset_restores_defaults(tmp_path: Path):
    """``reset()`` writes defaults and clears the window."""
    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    for _ in range(10):
        orch._on_decision(_DecisionEvt(decision="rejected"))
    # Confirm we moved off defaults.
    assert load_tuning(tmp_path) != DEFAULT_TUNING
    result = orch.reset()
    assert result.confidence_threshold == DEFAULT_TUNING.confidence_threshold
    assert orch.window_snapshot() == []
    orch.stop()


def test_orchestrator_langfuse_score_callback_invoked(tmp_path: Path):
    """A decision with a ``trace_id`` triggers the score callback."""
    captured: list[tuple[str, str]] = []

    def _score(trace_id: str, decision: str) -> None:
        captured.append((trace_id, decision))

    bus = _StubBus()
    orch = EvolutionOrchestrator(
        bus=bus, profile_home=tmp_path, langfuse_score_fn=_score
    )
    orch.start()

    orch._on_decision(
        _DecisionEvt(decision="accepted", trace_id="trace-abc")
    )

    assert captured == [("trace-abc", "accepted")]
    orch.stop()


def test_orchestrator_langfuse_no_trace_id_skips_callback(tmp_path: Path):
    """No ``trace_id`` → score callback not called."""
    captured: list = []

    def _score(*args) -> None:
        captured.append(args)

    bus = _StubBus()
    orch = EvolutionOrchestrator(
        bus=bus, profile_home=tmp_path, langfuse_score_fn=_score
    )
    orch.start()

    orch._on_decision(_DecisionEvt(decision="accepted"))  # trace_id=""

    assert captured == []
    orch.stop()


def test_orchestrator_handler_swallows_bad_event(tmp_path: Path):
    """A malformed event must not crash the handler or wedge the orchestrator."""

    class _Bad:
        decision = object()  # not a string-coercible value pathologically

    bus = _StubBus()
    orch = EvolutionOrchestrator(bus=bus, profile_home=tmp_path)
    orch.start()
    # int() on a plain object raises TypeError — handler must catch.
    orch._on_decision(_Bad())
    # Orchestrator state stays intact.
    snapshot = orch.window_snapshot()
    assert isinstance(snapshot, list)
    orch.stop()
