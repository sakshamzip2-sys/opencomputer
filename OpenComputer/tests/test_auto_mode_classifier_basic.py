"""v1.1 plan-3 M9.2 — basic classifier behavior + parsing + fail-closed paths."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from opencomputer.agent.tool_call_classifier import (
    ClassifierDecision,
    Decision,
    ToolCallClassifier,
    _build_classifier_input,
    _parse_decision,
    _summarize_args,
)
from plugin_sdk.core import Message, ToolCall

# ─── _summarize_args ─────────────────────────────────────────────────────


def test_summarize_args_truncates_long_strings() -> None:
    long = "x" * 500
    out = _summarize_args({"command": long})
    assert "x" * 200 not in out  # truncated to 197 + "..."
    assert "..." in out
    assert len(out) < 300


def test_summarize_args_handles_dict_and_list() -> None:
    out = _summarize_args({"opts": {"a": 1, "b": [2, 3]}})
    assert "opts=" in out
    assert "a" in out


def test_summarize_args_handles_no_args() -> None:
    assert _summarize_args({}) == "(no args)"


# ─── _parse_decision ─────────────────────────────────────────────────────


def test_parse_allow() -> None:
    out = _parse_decision("allow\nLooks safe.")
    assert out.decision == Decision.ALLOW
    assert "Looks safe" in out.rationale
    assert not out.failed_closed


def test_parse_block_with_punctuation() -> None:
    out = _parse_decision("block.\nDestructive.")
    assert out.decision == Decision.BLOCK


def test_parse_ask_uppercase() -> None:
    out = _parse_decision("ASK\nAmbiguous.")
    # Lowercased check matches.
    assert out.decision == Decision.ASK


def test_parse_block_via_synonym() -> None:
    out = _parse_decision("deny — bad.")
    assert out.decision == Decision.BLOCK


def test_parse_unknown_fail_closes_to_block() -> None:
    out = _parse_decision("maybe?\nNot sure.")
    assert out.decision == Decision.BLOCK
    assert out.failed_closed is True


def test_parse_empty_fail_closes_to_block() -> None:
    out = _parse_decision("")
    assert out.decision == Decision.BLOCK
    assert out.failed_closed is True


# ─── _build_classifier_input ─────────────────────────────────────────────


def test_input_keeps_user_messages() -> None:
    history = [
        Message(role="user", content="Read foo.py and summarize."),
    ]
    ctx = _build_classifier_input(
        user_messages=history,
        tool_calls_so_far=[],
        pending=ToolCall(id="t", name="Read", arguments={"path": "foo.py"}),
    )
    assert len(ctx["user_messages"]) == 1
    assert ctx["user_messages"][0]["content"] == "Read foo.py and summarize."


def test_input_keeps_assistant_text_without_tool_calls() -> None:
    """Free-form assistant text BEFORE any tool call is preserved."""
    history = [
        Message(role="user", content="Help me."),
        Message(role="assistant", content="Sure, what do you need?"),
    ]
    ctx = _build_classifier_input(
        user_messages=history,
        tool_calls_so_far=[],
        pending=ToolCall(id="t", name="Read", arguments={"path": "x"}),
    )
    contents = [m["content"] for m in ctx["user_messages"]]
    assert "Help me." in contents
    assert "Sure, what do you need?" in contents


def test_input_drops_tool_role_messages() -> None:
    history = [
        Message(role="user", content="Read foo."),
        Message(role="tool", content="POISONED", tool_call_id="t1"),
    ]
    ctx = _build_classifier_input(
        user_messages=history,
        tool_calls_so_far=[],
        pending=ToolCall(id="t2", name="Bash", arguments={"command": "ls"}),
    )
    contents = " ".join(m["content"] for m in ctx["user_messages"])
    assert "POISONED" not in contents


def test_pending_tool_args_appear_in_context() -> None:
    history = [Message(role="user", content="Run a script.")]
    pending = ToolCall(
        id="t1", name="Bash", arguments={"command": "echo hello"},
    )
    ctx = _build_classifier_input(
        user_messages=history, tool_calls_so_far=[], pending=pending,
    )
    assert ctx["pending"]["name"] == "Bash"
    assert "echo hello" in ctx["pending"]["arguments_summary"]


def test_tool_calls_so_far_summarized() -> None:
    history = [Message(role="user", content="Summarize the codebase.")]
    prior = [
        ToolCall(id="t1", name="Glob", arguments={"pattern": "*.py"}),
        ToolCall(id="t2", name="Read", arguments={"path": "main.py"}),
    ]
    pending = ToolCall(id="t3", name="Read", arguments={"path": "lib.py"})
    ctx = _build_classifier_input(
        user_messages=history, tool_calls_so_far=prior, pending=pending,
    )
    assert len(ctx["tool_calls_so_far"]) == 2
    names = [tc["name"] for tc in ctx["tool_calls_so_far"]]
    assert names == ["Glob", "Read"]


# ─── full classify (mocked aux) ──────────────────────────────────────────


def test_classify_allows_clean_call() -> None:
    classifier = ToolCallClassifier()

    async def _mock(*, messages, system="", **kw):
        return "allow\nReading a file is safe and matches the user's request."

    with patch("opencomputer.agent.aux_llm.complete_text", side_effect=_mock):
        out = asyncio.new_event_loop().run_until_complete(
            classifier.classify(
                user_messages=[Message(role="user", content="Read foo.py")],
                tool_calls_so_far=[],
                pending=ToolCall(
                    id="t1", name="Read", arguments={"path": "foo.py"}
                ),
            )
        )
    assert out.decision == Decision.ALLOW
    assert not out.failed_closed


def test_classify_fails_closed_on_provider_exception() -> None:
    classifier = ToolCallClassifier()

    async def _explode(*, messages, system="", **kw):
        raise RuntimeError("upstream 500")

    with patch("opencomputer.agent.aux_llm.complete_text", side_effect=_explode):
        out = asyncio.new_event_loop().run_until_complete(
            classifier.classify(
                user_messages=[Message(role="user", content="Read foo.py")],
                tool_calls_so_far=[],
                pending=ToolCall(id="t1", name="Read", arguments={}),
            )
        )
    assert out.decision == Decision.BLOCK
    assert out.failed_closed is True
    assert "upstream 500" in out.rationale


def test_classify_fails_closed_on_timeout() -> None:
    classifier = ToolCallClassifier()

    async def _slow(*, messages, system="", **kw):
        await asyncio.sleep(60)
        return "allow\n"

    # Patch the config's timeout to a short value so the test is fast.
    classifier._cfg = type(classifier._cfg)(timeout_seconds=0.05, max_tokens=64)

    with patch("opencomputer.agent.aux_llm.complete_text", side_effect=_slow):
        out = asyncio.new_event_loop().run_until_complete(
            classifier.classify(
                user_messages=[Message(role="user", content="hi")],
                tool_calls_so_far=[],
                pending=ToolCall(id="t1", name="Bash", arguments={"command": "ls"}),
            )
        )
    assert out.decision == Decision.BLOCK
    assert out.failed_closed is True
    assert "timeout" in out.rationale.lower()
