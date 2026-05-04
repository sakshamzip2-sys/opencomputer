"""Tests for opencomputer.agent.tool_guardrails — tool-loop streak detector.

Coexists with :class:`opencomputer.agent.loop_safety.LoopDetector` (sliding
window). The guard here uses a streak counter that trips faster on
deterministic tight loops with identical tool+args.
"""

from __future__ import annotations

import pytest

from opencomputer.agent.tool_guardrails import (
    GuardrailVerdict,
    ToolLoopGuard,
    ToolLoopGuardrailError,
)


def _call(name: str, **args):
    return {"name": name, "arguments": args}


def test_identical_repeats_warn_at_threshold():
    g = ToolLoopGuard(warn_at=3, stop_at=10)
    for _ in range(2):
        assert g.observe(_call("bash", command="ls")).level == "ok"
    v = g.observe(_call("bash", command="ls"))
    assert v.level == "warn"
    assert "bash" in v.message


def test_hard_stop_raises_at_stop_threshold():
    g = ToolLoopGuard(warn_at=3, stop_at=5)
    for _ in range(4):
        g.observe(_call("bash", command="ls"))
    with pytest.raises(ToolLoopGuardrailError) as exc:
        g.observe(_call("bash", command="ls"))
    assert "5" in str(exc.value)


def test_different_args_resets_streak():
    g = ToolLoopGuard(warn_at=3, stop_at=10)
    g.observe(_call("bash", command="ls"))
    g.observe(_call("bash", command="ls"))
    v = g.observe(_call("bash", command="pwd"))
    assert v.level == "ok"


def test_disabled_guard_never_warns_or_raises():
    g = ToolLoopGuard(warn_at=1, stop_at=2, enabled=False)
    for _ in range(50):
        v = g.observe(_call("bash", command="ls"))
        assert v.level == "ok"


def test_arg_order_normalized_via_canonical_json():
    g = ToolLoopGuard(warn_at=2, stop_at=10)
    g.observe(_call("bash", command="ls", cwd="/"))
    v = g.observe(_call("bash", cwd="/", command="ls"))
    assert v.level == "warn"


def test_reset_clears_streak():
    g = ToolLoopGuard(warn_at=2, stop_at=3)
    g.observe(_call("bash", command="ls"))
    g.observe(_call("bash", command="ls"))
    g.reset()
    v = g.observe(_call("bash", command="ls"))
    assert v.level == "ok"


def test_invalid_thresholds_rejected():
    with pytest.raises(ValueError):
        ToolLoopGuard(warn_at=0, stop_at=10)
    with pytest.raises(ValueError):
        ToolLoopGuard(warn_at=10, stop_at=5)


def test_verdict_is_immutable_dataclass():
    v = GuardrailVerdict(level="warn", message="x")
    assert v.level == "warn"
    assert v.message == "x"
    with pytest.raises(Exception):  # frozen dataclass
        v.level = "ok"  # type: ignore[misc]
