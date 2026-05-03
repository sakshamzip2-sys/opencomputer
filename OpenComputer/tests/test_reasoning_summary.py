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


# ─── v4: per-action descriptions ─────────────────────────────────────────


def test_summarize_tool_action_returns_clean_string():
    from opencomputer.agent.reasoning_summary import summarize_tool_action

    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        return_value=_fake_response("Wrote a haiku in foo.md"),
    ):
        out = summarize_tool_action(
            name="Edit", args_preview="file_path=foo.md, content=...", ok=True
        )
    assert out == "Wrote a haiku in foo.md"


def test_summarize_tool_action_returns_none_on_empty_name():
    from opencomputer.agent.reasoning_summary import summarize_tool_action

    out = summarize_tool_action(name="", args_preview="...", ok=True)
    assert out is None


def test_summarize_tool_action_returns_none_on_llm_failure():
    from opencomputer.agent.reasoning_summary import summarize_tool_action

    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        side_effect=RuntimeError("provider down"),
    ):
        out = summarize_tool_action(
            name="Edit", args_preview="file_path=x", ok=True
        )
    assert out is None


def test_summarize_tool_action_includes_failure_status():
    """When ok=False, the prompt should say 'and the call failed' so
    the model can produce a description like 'Tried to edit but failed'."""
    from opencomputer.agent.reasoning_summary import (
        _ACTION_PROMPT,
        summarize_tool_action,
    )

    captured = {"messages": None}

    def _capture(messages, **kwargs):
        captured["messages"] = messages
        return _fake_response("Tried to edit but failed")

    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        side_effect=_capture,
    ):
        out = summarize_tool_action(
            name="Edit", args_preview="file_path=x", ok=False
        )
    assert out == "Tried to edit but failed"
    msg = captured["messages"][0]["content"]
    assert "and the call failed" in msg
    assert _ACTION_PROMPT in msg


def test_maybe_describe_tool_actions_writes_each_to_store_via_daemon():
    from opencomputer.agent.reasoning_summary import maybe_describe_tool_actions
    from opencomputer.cli_ui.reasoning_store import ReasoningStore, ToolAction

    store = ReasoningStore()
    actions = [
        ToolAction(name="Edit", args_preview="file_path=a", ok=True, duration_s=0.1),
        ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.05),
    ]
    store.append(thinking="x", duration_s=0.1, tool_actions=actions)

    descriptions = iter(["Edited a", "Listed files"])

    def _per_call(messages, **kwargs):
        return _fake_response(next(descriptions))

    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        side_effect=_per_call,
    ):
        thread = maybe_describe_tool_actions(
            store=store, turn_id=1, actions=actions
        )
        assert thread is not None
        thread.join(timeout=10.0)

    t = store.get_by_id(1)
    assert t is not None
    assert t.tool_actions[0].description == "Edited a"
    assert t.tool_actions[1].description == "Listed files"


def test_maybe_describe_tool_actions_skips_when_no_actions():
    from opencomputer.agent.reasoning_summary import maybe_describe_tool_actions
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    store = ReasoningStore()
    thread = maybe_describe_tool_actions(
        store=store, turn_id=1, actions=[]
    )
    assert thread is None


def test_maybe_describe_tool_actions_swallows_individual_failures():
    """When ONE action's description fails, others still land."""
    from opencomputer.agent.reasoning_summary import maybe_describe_tool_actions
    from opencomputer.cli_ui.reasoning_store import ReasoningStore, ToolAction

    store = ReasoningStore()
    actions = [
        ToolAction(name="Edit", args_preview="file_path=a", ok=True, duration_s=0.1),
        ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.05),
    ]
    store.append(thinking="x", duration_s=0.1, tool_actions=actions)

    call_no = {"i": 0}

    def _flaky(messages, **kwargs):
        call_no["i"] += 1
        if call_no["i"] == 1:
            raise RuntimeError("Edit description failed")
        return _fake_response("Listed files")

    with patch(
        "opencomputer.agent.reasoning_summary.call_llm",
        side_effect=_flaky,
    ):
        thread = maybe_describe_tool_actions(
            store=store, turn_id=1, actions=actions
        )
        thread.join(timeout=10.0)

    t = store.get_by_id(1)
    assert t is not None
    assert t.tool_actions[0].description is None  # Edit failed → None
    assert t.tool_actions[1].description == "Listed files"  # Bash succeeded


def test_store_update_tool_description_unknown_turn_is_noop():
    """Defensive: store update for evicted turn doesn't crash."""
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    store = ReasoningStore()
    store.update_tool_description(turn_id=99, action_idx=0, description="x")
    assert store.get_all() == []


def test_store_update_tool_description_out_of_range_idx_is_noop():
    """Defensive: action_idx beyond the actual list is a no-op."""
    from opencomputer.cli_ui.reasoning_store import ReasoningStore, ToolAction

    store = ReasoningStore()
    store.append(
        thinking="x", duration_s=0.1,
        tool_actions=[
            ToolAction(name="Edit", args_preview="a", ok=True, duration_s=0.05),
        ],
    )
    store.update_tool_description(turn_id=1, action_idx=99, description="x")
    t = store.get_by_id(1)
    assert t is not None
    assert t.tool_actions[0].description is None  # untouched


def test_render_turn_tree_uses_description_when_available():
    """v4: tree shows the LLM description instead of generic tool
    name + args when available."""
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import (
        ReasoningStore,
        ToolAction,
        render_turn_tree,
    )

    store = ReasoningStore()
    store.append(
        thinking="x", duration_s=0.1,
        tool_actions=[
            ToolAction(
                name="Edit",
                args_preview="file_path=foo.md, content=hello",
                ok=True,
                duration_s=0.05,
                description="Wrote a haiku about sloths",
            ),
        ],
    )
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(
        render_turn_tree(store.get_latest())
    )
    text = out.getvalue()
    assert "Wrote a haiku about sloths" in text
    # File chip still shown as a child node when description is also
    # set — context for what file was touched.
    assert "foo.md" in text
