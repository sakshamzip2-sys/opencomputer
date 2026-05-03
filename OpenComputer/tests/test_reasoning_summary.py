"""Unit tests for the Haiku-powered reasoning summary generator."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from opencomputer.agent.reasoning_summary import (
    generate_summary,
    maybe_summarize_turn,
)
from opencomputer.cli_ui.reasoning_store import ReasoningStore


def _fake_response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def test_generate_summary_returns_clean_string():
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("Wrote a poem about sloths"),
    ):
        out = generate_summary("I should write a poem about sloths.")
    assert out == "Wrote a poem about sloths"


def test_generate_summary_strips_quotes_and_trailing_punctuation():
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response('"Wrote a poem about sloths."'),
    ):
        out = generate_summary("anything")
    assert out == "Wrote a poem about sloths"


def test_generate_summary_returns_none_on_empty_input():
    out = generate_summary("")
    assert out is None
    out = generate_summary("   \n  ")
    assert out is None


def test_generate_summary_returns_none_when_call_llm_raises():
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        side_effect=RuntimeError("provider down"),
    ):
        out = generate_summary("some thinking")
    assert out is None


def test_generate_summary_returns_none_on_empty_llm_response():
    """LLM returned empty string → don't store an empty summary."""
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response(""),
    ):
        out = generate_summary("anything")
    assert out is None


def test_generate_summary_caps_at_120_chars():
    long_str = "x" * 200
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response(long_str),
    ):
        out = generate_summary("anything")
    assert out is not None
    assert len(out) == 120


def test_maybe_summarize_turn_writes_to_store_via_daemon():
    store = ReasoningStore()
    store.append(thinking="reason", duration_s=0.1, tool_actions=[])

    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("Did the thing"),
    ):
        thread = maybe_summarize_turn(
            store=store, turn_id=1, thinking_text="reason"
        )
        assert thread is not None
        thread.join(timeout=5.0)

    t = store.get_by_id(1)
    assert t is not None
    assert t.summary == "Did the thing"


def test_maybe_summarize_turn_skips_when_no_thinking_text():
    """Tool-only turns have no thinking — skip the LLM call entirely."""
    store = ReasoningStore()
    store.append(thinking="", duration_s=0.1, tool_actions=[])
    thread = maybe_summarize_turn(
        store=store, turn_id=1, thinking_text=""
    )
    assert thread is None
    t = store.get_by_id(1)
    assert t is not None and t.summary is None


def test_maybe_summarize_turn_swallows_unknown_turn_id():
    """Defensive: if the turn was evicted by the time the summary
    arrives, the no-op update_summary path catches it."""
    store = ReasoningStore()
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("ignored"),
    ):
        thread = maybe_summarize_turn(
            store=store, turn_id=999, thinking_text="anything"
        )
        assert thread is not None
        thread.join(timeout=5.0)
    # No exception; store unchanged.
    assert store.get_all() == []


def test_maybe_summarize_turn_thread_is_daemon():
    """Daemon flag set so the process can exit even if the thread is
    still running."""
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("ok"),
    ):
        thread = maybe_summarize_turn(
            store=store, turn_id=1, thinking_text="x"
        )
        assert thread is not None
        assert thread.daemon is True
        thread.join(timeout=5.0)
