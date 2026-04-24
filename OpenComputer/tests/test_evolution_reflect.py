"""Tests for opencomputer.evolution.reflect — Insight dataclass and ReflectionEngine stub.

All tests are pure unit tests; no I/O, no mocks needed.
"""

from __future__ import annotations

import dataclasses

import pytest

from opencomputer.evolution.reflect import Insight, ReflectionEngine

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_VALID_INSIGHT_KWARGS = dict(
    observation="Agent repeats the same bash command three times.",
    evidence_refs=(1, 2, 3),
    action_type="create_skill",
    payload={"slug": "avoid-repeat-bash"},
    confidence=0.85,
)


def _make_insight(**overrides: object) -> Insight:
    kwargs = dict(_VALID_INSIGHT_KWARGS)
    kwargs.update(overrides)
    return Insight(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Frozen + slots
# ---------------------------------------------------------------------------


def test_insight_is_frozen_and_slots() -> None:
    """Insight is a frozen dataclass (immutable) and uses __slots__ for efficiency."""
    ins = _make_insight()

    # Frozen: assignment must raise FrozenInstanceError.
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        ins.observation = "changed"  # type: ignore[misc]

    # Slots: __slots__ defined means no __dict__ on the instance.
    assert not hasattr(ins, "__dict__"), "frozen+slots dataclass should not have __dict__"


# ---------------------------------------------------------------------------
# 2. Valid construction
# ---------------------------------------------------------------------------


def test_insight_valid_construction() -> None:
    """Build a valid Insight and verify field equality."""
    ins = _make_insight()

    assert ins.observation == "Agent repeats the same bash command three times."
    assert ins.evidence_refs == (1, 2, 3)
    assert ins.action_type == "create_skill"
    assert ins.payload == {"slug": "avoid-repeat-bash"}
    assert ins.confidence == pytest.approx(0.85)

    # Two identically constructed Insights compare equal.
    ins2 = _make_insight()
    assert ins == ins2


# ---------------------------------------------------------------------------
# 3. evidence_refs must be tuple
# ---------------------------------------------------------------------------


def test_insight_evidence_refs_must_be_tuple() -> None:
    """Passing a list for evidence_refs raises TypeError."""
    with pytest.raises(TypeError, match="tuple"):
        _make_insight(evidence_refs=[1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. confidence must be in [0.0, 1.0]
# ---------------------------------------------------------------------------


def test_insight_confidence_must_be_in_unit_interval() -> None:
    """confidence outside [0.0, 1.0] raises ValueError; boundary values 0.0 and 1.0 are fine."""
    with pytest.raises(ValueError, match="confidence"):
        _make_insight(confidence=1.5)

    with pytest.raises(ValueError, match="confidence"):
        _make_insight(confidence=-0.1)

    # Boundary values are accepted.
    lo = _make_insight(confidence=0.0)
    hi = _make_insight(confidence=1.0)
    assert lo.confidence == 0.0
    assert hi.confidence == 1.0


# ---------------------------------------------------------------------------
# 5. action_type must be one of the three Literals
# ---------------------------------------------------------------------------


def test_insight_action_type_must_be_valid() -> None:
    """action_type='garbage' raises ValueError; all three valid values are accepted."""
    with pytest.raises(ValueError, match="action_type"):
        _make_insight(action_type="garbage")  # type: ignore[arg-type]

    # All three valid values must work.
    for valid in ("create_skill", "edit_prompt", "noop"):
        ins = _make_insight(action_type=valid)  # type: ignore[arg-type]
        assert ins.action_type == valid


# ---------------------------------------------------------------------------
# 6. ReflectionEngine constructs with defaults
# ---------------------------------------------------------------------------


def test_reflection_engine_constructs_with_defaults() -> None:
    """ReflectionEngine(provider=<anything>) works; default window is 30."""
    engine = ReflectionEngine(provider=object())
    assert engine.window == 30


# ---------------------------------------------------------------------------
# 7. ReflectionEngine window override
# ---------------------------------------------------------------------------


def test_reflection_engine_window_override() -> None:
    """ReflectionEngine accepts an explicit window; .window property returns it."""
    engine = ReflectionEngine(provider=object(), window=10)
    assert engine.window == 10


# ---------------------------------------------------------------------------
# 8. window must be positive
# ---------------------------------------------------------------------------


def test_reflection_engine_window_must_be_positive() -> None:
    """window=0 and window=-5 both raise ValueError."""
    with pytest.raises(ValueError, match="window"):
        ReflectionEngine(provider=object(), window=0)

    with pytest.raises(ValueError, match="window"):
        ReflectionEngine(provider=object(), window=-5)


# ---------------------------------------------------------------------------
# 9. reflect() raises NotImplementedError mentioning B2
# ---------------------------------------------------------------------------


def test_reflect_raises_not_implemented() -> None:
    """engine.reflect([]) raises NotImplementedError with a message mentioning 'B2'."""
    engine = ReflectionEngine(provider=object())
    with pytest.raises(NotImplementedError, match="B2"):
        engine.reflect([])
