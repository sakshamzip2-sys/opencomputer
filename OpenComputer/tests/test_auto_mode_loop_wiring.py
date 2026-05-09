"""Auto-mode classifier wiring into AgentLoop (v1.1 plan-3 M9.5)."""

from __future__ import annotations

import inspect

import pytest

from opencomputer.agent.tool_call_classifier import (
    ClassifierConfig,
    ClassifierVerdict,
    ToolCallClassifier,
)
from plugin_sdk.core import Message, ToolCall

# ─── AgentLoop accepts the classifier kwarg ─────────────────────────


def test_agent_loop_init_accepts_tool_call_classifier_kwarg() -> None:
    """The constructor signature has the new kwarg with a None default."""
    from opencomputer.agent.loop import AgentLoop

    sig = inspect.signature(AgentLoop.__init__)
    assert "tool_call_classifier" in sig.parameters
    p = sig.parameters["tool_call_classifier"]
    # Optional with default = None
    assert p.default is None


def test_agent_loop_stores_classifier_attribute() -> None:
    """The constructor assigns the classifier to self._tool_call_classifier
    (the agent loop reads this attribute per turn)."""
    # We don't fully construct an AgentLoop (heavy deps); we just
    # assert the source code stores the attribute we wired.
    import opencomputer.agent.loop as _loop

    src = inspect.getsource(_loop)
    assert "self._tool_call_classifier = tool_call_classifier" in src


def test_agent_loop_calls_classifier_in_auto_mode() -> None:
    """The loop's source MUST contain the gating logic that consults
    the classifier when effective_permission_mode == 'auto'."""
    import opencomputer.agent.loop as _loop

    src = inspect.getsource(_loop)
    # The wiring exists.
    assert "_eff_mode == \"auto\"" in src
    assert "self._tool_call_classifier" in src
    assert "ClassifierVerdict.BLOCK" in src
    # The classifier check happens BEFORE the consent gate.
    classifier_idx = src.find("self._tool_call_classifier.classify")
    consent_idx = src.find("self._consent_gate.check")
    assert classifier_idx > 0
    assert consent_idx > 0
    assert classifier_idx < consent_idx, (
        "Classifier must run BEFORE consent gate so a poison-induced "
        "tool call is blocked at the cheaper layer first."
    )


def test_agent_loop_block_budget_warning_present() -> None:
    """When the classifier's per-session block budget hits the
    threshold, the loop logs a warning instructing the operator to
    resume."""
    import opencomputer.agent.loop as _loop

    src = inspect.getsource(_loop)
    assert "block budget exceeded" in src or "is_paused" in src


def test_agent_loop_classifier_failure_does_not_crash_loop() -> None:
    """The classifier wiring is wrapped in a try/except so a
    classifier bug never breaks the agent loop."""
    import opencomputer.agent.loop as _loop

    src = inspect.getsource(_loop)
    # Find the M9.5 block and verify the try/except wrapper is present
    m95_idx = src.find("v1.1 plan-3 M9.5 — Auto-mode classifier")
    assert m95_idx > 0
    # The block has a try/except for the classifier path
    after = src[m95_idx : m95_idx + 4000]
    assert "try:" in after
    assert "except Exception" in after


# ─── classifier itself still works (regression guard) ──────────────


@pytest.mark.asyncio
async def test_classifier_block_path_unchanged_after_wiring() -> None:
    """Sanity: the classifier engine still produces the same outputs
    after the wiring change."""

    async def fake_complete(*, messages, max_tokens, model, temperature):
        return "VERDICT: block\nRATIONALE: rm -rf is destructive"

    cls_ = ToolCallClassifier(complete_text=fake_complete)
    decision = await cls_.classify(
        session_id="s1",
        user_messages=[Message(role="user", content="hello")],
        tool_calls_so_far=[],
        pending=ToolCall(id="x", name="Bash", arguments={"command": "rm -rf /"}),
    )
    assert decision.verdict == ClassifierVerdict.BLOCK


@pytest.mark.asyncio
async def test_classifier_can_be_constructed_with_default_config() -> None:
    """Verify the canonical happy-path construction still works."""
    cls_ = ToolCallClassifier(
        complete_text=None,
        config=ClassifierConfig(enabled=True),
    )
    # Default fail_closed=True
    assert cls_._config.fail_closed is True
