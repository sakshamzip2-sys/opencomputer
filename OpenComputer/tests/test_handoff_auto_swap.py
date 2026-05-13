"""Tests for opencomputer.agent.handoff.auto_swap.AutoSwapTrigger."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from opencomputer.agent.handoff.auto_swap import (
    AutoSwapTrigger,
    SwapDecision,
    SwapDecisionReason,
)
from opencomputer.awareness.personas.classifier import ClassificationResult


@dataclass
class FakeRuntime:
    custom: dict[str, Any] = field(default_factory=dict)


def _cls(persona: str, confidence: float, reason: str = "") -> ClassificationResult:
    return ClassificationResult(
        persona_id=persona, confidence=confidence, reason=reason or persona,
    )


def _trigger(**overrides: Any) -> AutoSwapTrigger:
    """Build a trigger with a deterministic persona→profile resolver."""
    persona_map = {"trading": "stocks", "coding": "coder"}

    def resolver(persona: str, available: tuple[str, ...]) -> str | None:
        candidate = persona_map.get(persona)
        return candidate if candidate in available else None

    kwargs: dict[str, Any] = {"persona_to_profile": resolver}
    kwargs.update(overrides)
    return AutoSwapTrigger(**kwargs)


def _eval_default(trigger: AutoSwapTrigger, rt: FakeRuntime, cls: ClassificationResult,
                  current: str = "default", available: tuple[str, ...] = ("stocks", "coder")):
    return trigger.evaluate(
        runtime=rt,
        session_id="sess-A",
        classification=cls,
        current_profile=current,
        available_profiles=available,
        plan_mode=False,
        auto_off=False,
        is_gateway_session=False,
        gateway_optin=False,
    )


class TestStreakBehavior:
    def test_single_high_confidence_turn_does_not_fire(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        decision = _eval_default(trigger, rt, _cls("trading", 0.9))
        assert not decision.should_swap
        assert decision.reason == SwapDecisionReason.STREAK_INCOMPLETE

    def test_two_consecutive_turns_does_not_fire(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        _eval_default(trigger, rt, _cls("trading", 0.9))
        decision = _eval_default(trigger, rt, _cls("trading", 0.9))
        assert not decision.should_swap
        assert decision.reason == SwapDecisionReason.STREAK_INCOMPLETE

    def test_three_consecutive_high_confidence_fires(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        for _ in range(2):
            _eval_default(trigger, rt, _cls("trading", 0.9))
        decision = _eval_default(trigger, rt, _cls("trading", 0.9))
        assert decision.should_swap
        assert decision.target_profile == "stocks"
        assert decision.reason == SwapDecisionReason.FIRED

    def test_mixed_personas_in_window_does_not_fire(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        _eval_default(trigger, rt, _cls("trading", 0.9))
        _eval_default(trigger, rt, _cls("coding", 0.9))
        decision = _eval_default(trigger, rt, _cls("trading", 0.9))
        assert not decision.should_swap
        assert decision.reason == SwapDecisionReason.MIXED_PERSONAS

    def test_low_confidence_in_streak_blocks(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        _eval_default(trigger, rt, _cls("trading", 0.9))
        _eval_default(trigger, rt, _cls("trading", 0.4))  # low
        decision = _eval_default(trigger, rt, _cls("trading", 0.9))
        assert not decision.should_swap
        # The 0.4 turn triggers the below-threshold path on its own turn,
        # but on this turn the tail's MIN is still 0.4 → below threshold.
        assert decision.reason == SwapDecisionReason.BELOW_THRESHOLD


class TestCooldown:
    def test_cooldown_blocks_immediate_re_swap(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        for _ in range(3):
            _eval_default(trigger, rt, _cls("trading", 0.9))
        # Fire one
        first = _eval_default(trigger, rt, _cls("trading", 0.9))
        assert first.should_swap
        trigger.mark_swapped(runtime=rt, session_id="sess-A")
        # Next 5 turns should be blocked by cooldown
        for _ in range(5):
            d = _eval_default(trigger, rt, _cls("coding", 0.95), current="stocks")
            assert not d.should_swap
            assert d.reason == SwapDecisionReason.COOLDOWN_ACTIVE

    def test_cooldown_clears_after_n_turns(self) -> None:
        trigger = _trigger(cooldown_turns=2)
        rt = FakeRuntime()
        trigger.mark_swapped(runtime=rt, session_id="sess-A")
        for _ in range(2):
            d = _eval_default(trigger, rt, _cls("trading", 0.5))
            assert d.reason == SwapDecisionReason.COOLDOWN_ACTIVE
        # Now cooldown is 0 again — but streak is incomplete because
        # cooldown evals also advanced the window with low-confidence
        # results; verify the gate is gone
        assert trigger.cooldown_remaining(runtime=rt, session_id="sess-A") == 0

    def test_manual_mark_swapped_resets_cooldown(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        trigger.mark_swapped(runtime=rt, session_id="sess-A")
        assert trigger.cooldown_remaining(runtime=rt, session_id="sess-A") == 5


class TestGates:
    def test_plan_mode_blocks(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        decision = trigger.evaluate(
            runtime=rt,
            session_id="sess-A",
            classification=_cls("trading", 0.95),
            current_profile="default",
            available_profiles=("stocks",),
            plan_mode=True,
            auto_off=False,
            is_gateway_session=False,
            gateway_optin=False,
        )
        assert not decision.should_swap
        assert decision.reason == SwapDecisionReason.PLAN_MODE

    def test_auto_off_blocks(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        decision = trigger.evaluate(
            runtime=rt, session_id="sess-A",
            classification=_cls("trading", 0.95),
            current_profile="default", available_profiles=("stocks",),
            plan_mode=False, auto_off=True,
            is_gateway_session=False, gateway_optin=False,
        )
        assert not decision.should_swap
        assert decision.reason == SwapDecisionReason.AUTO_OFF

    def test_gateway_without_optin_blocks(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        decision = trigger.evaluate(
            runtime=rt, session_id="sess-A",
            classification=_cls("trading", 0.95),
            current_profile="default", available_profiles=("stocks",),
            plan_mode=False, auto_off=False,
            is_gateway_session=True, gateway_optin=False,
        )
        assert not decision.should_swap
        assert decision.reason == SwapDecisionReason.GATEWAY_DISABLED

    def test_gateway_with_optin_allowed_through(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        for _ in range(2):
            trigger.evaluate(
                runtime=rt, session_id="sess-A",
                classification=_cls("trading", 0.9),
                current_profile="default", available_profiles=("stocks",),
                plan_mode=False, auto_off=False,
                is_gateway_session=True, gateway_optin=True,
            )
        decision = trigger.evaluate(
            runtime=rt, session_id="sess-A",
            classification=_cls("trading", 0.9),
            current_profile="default", available_profiles=("stocks",),
            plan_mode=False, auto_off=False,
            is_gateway_session=True, gateway_optin=True,
        )
        assert decision.should_swap

    def test_default_persona_never_targets(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        for _ in range(3):
            decision = _eval_default(trigger, rt, _cls("default", 0.99))
        assert not decision.should_swap
        assert decision.reason in (
            SwapDecisionReason.PERSONA_UNMAPPED,
            SwapDecisionReason.NO_AVAILABLE_TARGET,
        )

    def test_persona_unmapped_to_profile_skipped(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        for _ in range(3):
            d = trigger.evaluate(
                runtime=rt, session_id="sess-A",
                classification=_cls("trading", 0.9),
                current_profile="default",
                available_profiles=(),  # no profiles at all
                plan_mode=False, auto_off=False,
                is_gateway_session=False, gateway_optin=False,
            )
        assert not d.should_swap
        assert d.reason == SwapDecisionReason.NO_AVAILABLE_TARGET

    def test_persona_resolves_to_current_skipped(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        for _ in range(3):
            d = trigger.evaluate(
                runtime=rt, session_id="sess-A",
                classification=_cls("trading", 0.9),
                current_profile="stocks",  # already on the target
                available_profiles=("stocks",),
                plan_mode=False, auto_off=False,
                is_gateway_session=False, gateway_optin=False,
            )
        assert not d.should_swap
        assert d.reason == SwapDecisionReason.PERSONA_IS_CURRENT


class TestSessionIsolation:
    def test_two_sessions_have_independent_windows(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        # Session A: 3 stocks turns
        for _ in range(2):
            trigger.evaluate(
                runtime=rt, session_id="A",
                classification=_cls("trading", 0.9),
                current_profile="default", available_profiles=("stocks",),
                plan_mode=False, auto_off=False,
                is_gateway_session=False, gateway_optin=False,
            )
        # Session B: 1 stocks turn — should NOT swap
        d_b = trigger.evaluate(
            runtime=rt, session_id="B",
            classification=_cls("trading", 0.9),
            current_profile="default", available_profiles=("stocks",),
            plan_mode=False, auto_off=False,
            is_gateway_session=False, gateway_optin=False,
        )
        assert not d_b.should_swap


class TestConstructorValidation:
    def test_streak_length_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="streak_length"):
            AutoSwapTrigger(streak_length=0)

    def test_confidence_threshold_must_be_in_range(self) -> None:
        with pytest.raises(ValueError, match="confidence_threshold"):
            AutoSwapTrigger(confidence_threshold=0.0)
        with pytest.raises(ValueError, match="confidence_threshold"):
            AutoSwapTrigger(confidence_threshold=1.1)

    def test_cooldown_cannot_be_negative(self) -> None:
        with pytest.raises(ValueError, match="cooldown_turns"):
            AutoSwapTrigger(cooldown_turns=-1)

    def test_window_must_be_at_least_streak(self) -> None:
        with pytest.raises(ValueError, match="window_size"):
            AutoSwapTrigger(window_size=2, streak_length=3)

    def test_evaluate_rejects_empty_session_id(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        with pytest.raises(ValueError, match="session_id"):
            trigger.evaluate(
                runtime=rt, session_id="",
                classification=_cls("trading", 0.9),
                current_profile="default", available_profiles=(),
                plan_mode=False, auto_off=False,
                is_gateway_session=False, gateway_optin=False,
            )

    def test_evaluate_rejects_wrong_classification_type(self) -> None:
        trigger = _trigger()
        rt = FakeRuntime()
        with pytest.raises(TypeError):
            trigger.evaluate(
                runtime=rt, session_id="A",
                classification="not a result",  # type: ignore[arg-type]
                current_profile="default", available_profiles=(),
                plan_mode=False, auto_off=False,
                is_gateway_session=False, gateway_optin=False,
            )
