"""Direct integration tests for the load-bearing evolution-loop wires.

Cover the gaps the high-level E2E test glosses over:

1. The skill-evolution subscriber actually USES the tuned threshold
   (not just reads it) — proved by injecting a high tuning value
   and confirming the subscriber rejects a borderline judge result.
2. ``record_llm_call`` auto-fills ``trace_id`` from the contextvar
   when the caller doesn't supply one — verifies the
   per-turn-correlation wire works end-to-end.
3. The CLI-mode singleton orchestrator path actually subscribes to
   the bus and consumes decisions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.evolution_orchestrator import (
    DEFAULT_TUNING,
    EvolutionOrchestrator,
    get_or_start_orchestrator,
    load_tuning,
    shutdown_singleton,
)
from opencomputer.inference.observability import (
    LLMCallEvent,
    record_llm_call,
    register_subscriber,
    unregister_subscriber,
)
from opencomputer.observability.trace import (
    reset_trace_id,
    set_trace_id,
    trace_scope,
)
from plugin_sdk.ingestion import (
    SessionEndEvent,
    SkillReviewDecisionEvent,
)

# ─── Gap 5: record_llm_call auto-fills trace_id from contextvar ──────


def test_record_llm_call_auto_fills_trace_id_from_contextvar(
    tmp_path: Path, monkeypatch
):
    """When the provider constructs LLMCallEvent without trace_id, the
    ``record_llm_call`` sink should rebind it from the contextvar.

    This is the wire that makes per-turn correlation work without
    every provider knowing about the trace module.
    """
    # Redirect the JSONL log under tmp so we don't pollute the user's
    # real profile dir.
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    received: list[LLMCallEvent] = []

    def _sub(event: LLMCallEvent) -> None:
        received.append(event)

    register_subscriber(_sub)
    try:
        # No active trace → trace_id stays None.
        record_llm_call(
            LLMCallEvent(
                ts=datetime.now(),
                provider="test",
                model="test-model",
                input_tokens=10,
                output_tokens=20,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                latency_ms=100,
                cost_usd=0.001,
                site="unit_test",
            )
        )
        assert received[-1].trace_id is None

        # Inside trace_scope → trace_id auto-filled.
        with trace_scope("wire-test-tid"):
            record_llm_call(
                LLMCallEvent(
                    ts=datetime.now(),
                    provider="test",
                    model="test-model",
                    input_tokens=5,
                    output_tokens=10,
                    cache_creation_tokens=0,
                    cache_read_tokens=0,
                    latency_ms=50,
                    cost_usd=0.0005,
                    site="unit_test_scoped",
                )
            )
        assert received[-1].trace_id == "wire-test-tid"
    finally:
        unregister_subscriber(_sub)


def test_record_llm_call_honors_explicit_trace_id(tmp_path: Path, monkeypatch):
    """When the caller supplies a trace_id, the contextvar is NOT
    consulted — explicit beats ambient."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))

    received: list[LLMCallEvent] = []
    register_subscriber(received.append)
    try:
        with trace_scope("ambient-tid"):
            record_llm_call(
                LLMCallEvent(
                    ts=datetime.now(),
                    provider="p",
                    model="m",
                    input_tokens=1,
                    output_tokens=1,
                    cache_creation_tokens=0,
                    cache_read_tokens=0,
                    latency_ms=1,
                    cost_usd=None,
                    site="x",
                    trace_id="explicit-tid",
                )
            )
        assert received[-1].trace_id == "explicit-tid"
    finally:
        unregister_subscriber(received.append)


# ─── Gap 4: subscriber uses tuned_threshold value ────────────────────


def _load_subscriber_module():
    """Synthetic-module load for ``extensions/skill-evolution/`` (hyphenated)."""
    cache_key = "skill_evolution_subscriber_wire_test"
    if cache_key in sys.modules:
        return sys.modules[cache_key]

    se_dir = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "skill-evolution"
    )
    # Set up the package alias the subscriber's relative imports need.
    if "extensions.skill_evolution" not in sys.modules:
        import types

        if "extensions" not in sys.modules:
            ext_root = types.ModuleType("extensions")
            ext_root.__path__ = [str(se_dir.parent)]
            sys.modules["extensions"] = ext_root
        pkg = types.ModuleType("extensions.skill_evolution")
        pkg.__path__ = [str(se_dir)]
        pkg.__package__ = "extensions.skill_evolution"
        sys.modules["extensions.skill_evolution"] = pkg

        # Pre-load the modules subscriber.py imports relatively.
        for sub in (
            "candidate_store",
            "pattern_detector",
            "session_metrics",
            "skill_extractor",
        ):
            full_name = f"extensions.skill_evolution.{sub}"
            if full_name in sys.modules:
                continue
            spec = importlib.util.spec_from_file_location(
                full_name, se_dir / f"{sub}.py"
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = "extensions.skill_evolution"
            sys.modules[full_name] = mod
            spec.loader.exec_module(mod)

    spec = importlib.util.spec_from_file_location(
        "extensions.skill_evolution.subscriber",
        se_dir / "subscriber.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "extensions.skill_evolution"
    sys.modules["extensions.skill_evolution.subscriber"] = mod
    sys.modules[cache_key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_subscriber_rejects_when_tuned_threshold_above_judge_confidence(
    tmp_path: Path,
):
    """When the tuning file holds a high confidence threshold, a
    judge result that beats the ctor default (70) but not the tuned
    value (85) must be SKIPPED — the proposal is not extracted.

    This proves the subscriber actually reads + applies the tuned
    threshold, not just reads it and then ignores.
    """
    # Persist a tuned threshold of 85.
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "confidence_threshold": 85,
                "dreaming_v2_score_threshold": 0.65,
                "dreaming_v2_min_recall": 2,
                "decisions_observed": 30,
                "last_recompute_ts": time.time(),
            }
        )
    )

    sub_mod = _load_subscriber_module()
    EvolutionSubscriber = sub_mod.EvolutionSubscriber

    # Build stubs. The pipeline path:
    #   compute_session_metrics → is_candidate_session → judge_candidate_async → extractor
    # We need Stage 1 to pass and the judge to return confidence=80 (above
    # default 70, below tuned 85). The pipeline should reject at the
    # threshold check.
    extractor_called: list[bool] = []

    async def _judge_stub(score, **kwargs):  # noqa: ARG001
        # 80 confidence, novel — beats default 70, loses to tuned 85.
        return MagicMock(confidence=80, is_novel=True, reason="test")

    async def _extract_stub(*args, **kwargs):  # noqa: ARG001
        extractor_called.append(True)
        return MagicMock(name="should-not-fire")

    # Patch the module-level functions used inside _run_pipeline_inner.
    import opencomputer.agent.evolution_orchestrator as evo_mod

    sub_mod.judge_candidate_async = _judge_stub
    sub_mod.extract_skill_from_session = _extract_stub

    # Force Stage 1 to pass with a fake metrics result.
    fake_metrics = MagicMock(
        user_messages_total_chars=500,
        user_messages_concat="user wants thing X",
        tool_calls=[],
        turn_count=5,
    )
    sub_mod.compute_session_metrics = lambda db, sid: fake_metrics
    sub_mod.is_candidate_session = lambda *a, **kw: MagicMock(
        is_candidate=True,
        rejection_reason="",
        session_id="sess",
        turn_count=5,
        summary_hint="",
    )

    subscriber = EvolutionSubscriber(
        bus=MagicMock(),
        profile_home_factory=lambda: tmp_path,
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
        confidence_threshold=70,  # Default — would have accepted at 80
    )

    event = SessionEndEvent(
        session_id="sess",
        end_reason="completed",
        turn_count=5,
        duration_seconds=10.0,
        had_errors=False,
    )

    await subscriber._run_pipeline_inner(event)

    # Confirm the loaded tuning is what we expect, then verify
    # the extractor was NOT called.
    assert load_tuning(tmp_path).confidence_threshold == 85
    assert extractor_called == [], (
        "extractor should be skipped because tuned threshold=85 > judge=80"
    )


@pytest.mark.asyncio
async def test_subscriber_accepts_when_judge_beats_tuned_threshold(
    tmp_path: Path,
):
    """Inverse: with tuned threshold=70 and judge confidence=80, the
    extractor IS invoked. Sanity check the threshold gate logic isn't
    inverted."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "confidence_threshold": 70,
                "dreaming_v2_score_threshold": 0.65,
                "dreaming_v2_min_recall": 2,
                "decisions_observed": 0,
                "last_recompute_ts": 0.0,
            }
        )
    )

    sub_mod = _load_subscriber_module()
    EvolutionSubscriber = sub_mod.EvolutionSubscriber

    extractor_called: list[bool] = []

    async def _judge_stub(score, **kwargs):  # noqa: ARG001
        return MagicMock(confidence=80, is_novel=True, reason="test")

    async def _extract_stub(*args, **kwargs):  # noqa: ARG001
        extractor_called.append(True)
        return None  # extractor None → store not invoked, that's fine

    sub_mod.judge_candidate_async = _judge_stub
    sub_mod.extract_skill_from_session = _extract_stub
    sub_mod.compute_session_metrics = lambda db, sid: MagicMock(
        user_messages_total_chars=500,
        user_messages_concat="x",
        tool_calls=[],
    )
    sub_mod.is_candidate_session = lambda *a, **kw: MagicMock(
        is_candidate=True,
        rejection_reason="",
        session_id="sess",
        turn_count=5,
        summary_hint="",
    )

    subscriber = EvolutionSubscriber(
        bus=MagicMock(),
        profile_home_factory=lambda: tmp_path,
        session_db_factory=lambda: MagicMock(),
        provider=MagicMock(),
        cost_guard=MagicMock(),
        confidence_threshold=70,
    )

    event = SessionEndEvent(
        session_id="sess",
        end_reason="completed",
        turn_count=5,
        duration_seconds=10.0,
        had_errors=False,
    )

    await subscriber._run_pipeline_inner(event)

    assert extractor_called == [True]


# ─── Gap 3: CLI-mode singleton orchestrator wires up ─────────────────


def test_singleton_orchestrator_subscribes_in_cli_mode(tmp_path: Path):
    """``get_or_start_orchestrator`` returns a started instance that
    actually receives bus events. Standalone CLI path."""
    # Reset any previous singleton from earlier tests.
    shutdown_singleton()

    # The singleton reads default_bus and the live profile. We can't
    # easily monkeypatch _home() across the lazy import, so build the
    # singleton with an explicit profile_home.
    orchestrator = get_or_start_orchestrator(profile_home=tmp_path)
    assert orchestrator is not None

    # Re-invoke — should return the same instance (singleton).
    assert get_or_start_orchestrator(profile_home=tmp_path) is orchestrator

    # Publish a decision on the SAME default bus the singleton uses.
    from opencomputer.ingestion.bus import default_bus

    default_bus.publish(
        SkillReviewDecisionEvent(
            source="test",
            skill_name="auto-cli-test",
            decision="rejected",
            origin_session_id="sess-cli",
            confidence_at_proposal=72,
        )
    )

    # Orchestrator should have observed it.
    snap = orchestrator.window_snapshot()
    assert len(snap) >= 1
    assert any(
        r.skill_name == "auto-cli-test" and r.decision == "rejected"
        for r in snap
    )

    # Clean up.
    shutdown_singleton()
    assert get_or_start_orchestrator.__name__  # smoke that it's still callable


def test_singleton_shutdown_clears_handle(tmp_path: Path):
    """``shutdown_singleton`` makes the next call return a NEW instance."""
    shutdown_singleton()

    a = get_or_start_orchestrator(profile_home=tmp_path)
    assert a is not None

    shutdown_singleton()

    b = get_or_start_orchestrator(profile_home=tmp_path)
    assert b is not None
    assert a is not b, "shutdown should force a fresh instance"

    shutdown_singleton()
