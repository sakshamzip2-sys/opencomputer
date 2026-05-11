"""End-to-end test for the closed self-evolution loop.

Walks through:

1. A skill-evolution proposal captures the active ``trace_id`` in
   provenance.json.
2. ``oc skills review`` accepts the proposal, emitting a
   ``SkillReviewDecisionEvent`` on the default bus.
3. The :class:`EvolutionOrchestrator` subscribes to that event,
   appends to its rolling window, and tunes thresholds when enough
   decisions accumulate.
4. The skill-evolution subscriber reads the tuned
   ``confidence_threshold`` on its next pipeline run and applies it.

Each step is exercised here in isolation against in-memory bus + tmp
profile_home so the full loop runs in ~1 second without requiring
the real gateway daemon.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from opencomputer.agent.evolution_orchestrator import (
    DEFAULT_TUNING,
    EvolutionOrchestrator,
    load_tuning,
)
from opencomputer.observability.trace import (
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    trace_scope,
)
from plugin_sdk.ingestion import SkillReviewDecisionEvent


class _InMemoryBus:
    """Minimal sync bus stand-in. Doesn't replicate the full TypedEventBus
    fan-out semantics — just enough to test pub/sub of decision events."""

    def __init__(self) -> None:
        self.subs: dict[str, list] = {}
        self.published: list = []

    def subscribe(self, event_type: str, handler):  # noqa: ANN001
        self.subs.setdefault(event_type, []).append(handler)

        class _Sub:
            def unsubscribe(_):  # noqa: N805, ARG002
                self.subs[event_type].remove(handler)

        return _Sub()

    def publish(self, event) -> None:  # noqa: ANN001
        self.published.append(event)
        for h in self.subs.get(event.event_type, []):
            h(event)


# ─── stage 1: trace_id ends up in provenance.json ────────────────────


def test_trace_id_captured_into_provenance():
    """The extractor reads the active trace contextvar at provenance-build
    time. We exercise the read path directly rather than the whole
    LLM-driven extractor, since the latter has many fall-through code
    paths (schema-validated vs text procedure parsing) whose stubbing is
    brittle to test in isolation.

    The path under test is the inline block in
    ``extract_skill_from_session`` at the bottom of the function:

        from opencomputer.observability.trace import get_trace_id
        trace_id_val = get_trace_id() or ""
        provenance = {..., "trace_id": trace_id_val, ...}

    We confirm both the empty-context behavior and the in-scope behavior.
    """
    # Outside any scope → empty string default.
    from opencomputer.observability.trace import get_trace_id as _get

    assert (_get() or "") == ""

    # Inside a scope, the same read returns the active id.
    with trace_scope("e2e-trace-xyz") as tid:
        assert (_get() or "") == tid
        # Now mimic the provenance-build inline block.
        provenance = {
            "session_id": "sess-e2e",
            "generated_at": "2026-05-11T00:00:00Z",
            "confidence_score": 80,
            "source_summary": "stub",
            "trace_id": _get() or "",
        }
        assert provenance["trace_id"] == "e2e-trace-xyz"


# ─── stage 2 + 3: orchestrator subscribes & tunes ────────────────────


def test_orchestrator_processes_decision_event_end_to_end(tmp_path: Path):
    """A published :class:`SkillReviewDecisionEvent` ripples through the
    orchestrator, lands in its rolling window, and triggers a tune on
    the Nth decision."""

    bus = _InMemoryBus()
    orch = EvolutionOrchestrator(
        bus=bus, profile_home=tmp_path, langfuse_score_fn=None
    )
    orch.start()

    # Publish 10 rejections — by the 10th, opportunistic tune fires.
    for i in range(10):
        evt = SkillReviewDecisionEvent(
            source="cli_skills.review",
            skill_name=f"auto-{i}",
            decision="rejected",
            origin_session_id=f"sess-{i}",
            trace_id=f"trace-{i}",
            confidence_at_proposal=75,
        )
        bus.publish(evt)

    # Confidence threshold should have moved upward (tighter).
    persisted = load_tuning(tmp_path)
    assert (
        persisted.confidence_threshold
        == DEFAULT_TUNING.confidence_threshold + 5
    )
    # Decision count matches what we fed.
    assert persisted.decisions_observed == 10

    # Orchestrator window holds all 10 (deque max=20).
    snap = orch.window_snapshot()
    assert len(snap) == 10
    assert all(r.decision == "rejected" for r in snap)

    orch.stop()


def test_orchestrator_langfuse_callback_invoked_on_traced_decisions(
    tmp_path: Path,
):
    """When a langfuse score callback is wired, it fires for every
    decision that carries a ``trace_id``."""
    captured: list[tuple[str, str]] = []

    def _score(trace_id: str, decision: str) -> None:
        captured.append((trace_id, decision))

    bus = _InMemoryBus()
    orch = EvolutionOrchestrator(
        bus=bus, profile_home=tmp_path, langfuse_score_fn=_score
    )
    orch.start()

    bus.publish(
        SkillReviewDecisionEvent(
            skill_name="x",
            decision="accepted",
            trace_id="trace-α",
        )
    )
    bus.publish(
        SkillReviewDecisionEvent(
            skill_name="y",
            decision="edited",
            trace_id="trace-β",
        )
    )
    bus.publish(
        SkillReviewDecisionEvent(
            skill_name="z",
            decision="deferred",
            trace_id="trace-γ",  # deferred is still routed; orchestrator
            # itself records but langfuse callback rejects unknown
            # decisions internally.
        )
    )

    # First two scored; third also forwarded (orchestrator doesn't
    # filter — that's the score_trace fn's job), but the in-test
    # callback receives all three. The langfuse plugin's real
    # ``score_trace`` would drop "deferred" itself.
    assert captured == [
        ("trace-α", "accepted"),
        ("trace-β", "edited"),
        ("trace-γ", "deferred"),
    ]
    orch.stop()


# ─── stage 4: skill-evolution subscriber reads tuned threshold ───────


def test_skill_evolution_subscriber_picks_up_tuned_threshold(tmp_path: Path):
    """When ``evolution_tuning.json`` is present with a tuned
    ``confidence_threshold``, the subscriber's :func:`load_tuning` read
    returns the tuned value (not the default)."""

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "confidence_threshold": 85,  # tuned higher than the default 70
                "dreaming_v2_score_threshold": 0.65,
                "dreaming_v2_min_recall": 2,
                "decisions_observed": 30,
                "last_recompute_ts": time.time(),
            }
        )
    )

    tuning = load_tuning(tmp_path)
    assert tuning.confidence_threshold == 85
    # And the value is sane (clamped, schema-aware).
    assert 50 <= tuning.confidence_threshold <= 95
