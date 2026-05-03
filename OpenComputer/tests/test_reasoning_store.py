"""Unit tests for ReasoningStore (per-session in-memory store)."""
from __future__ import annotations

import pytest

from opencomputer.cli_ui.reasoning_store import (
    ReasoningStore,
    ReasoningTurn,
    ToolAction,
)


def test_store_starts_empty():
    store = ReasoningStore()
    assert store.get_all() == []
    assert store.get_latest() is None


def test_append_assigns_monotonic_turn_ids():
    store = ReasoningStore()
    t1 = store.append(thinking="first", duration_s=0.5, tool_actions=[])
    t2 = store.append(thinking="second", duration_s=1.2, tool_actions=[])
    assert t1.turn_id == 1
    assert t2.turn_id == 2
    assert store.get_latest() is t2


def test_get_by_id_returns_match_or_none():
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    store.append(thinking="y", duration_s=0.2, tool_actions=[])
    first = store.get_by_id(1)
    second = store.get_by_id(2)
    assert first is not None
    assert first.thinking == "x"
    assert second is not None
    assert second.thinking == "y"
    assert store.get_by_id(99) is None


def test_store_caps_to_max_turns():
    store = ReasoningStore(max_turns=3)
    for i in range(5):
        store.append(thinking=f"t{i}", duration_s=0.1, tool_actions=[])
    all_turns = store.get_all()
    assert len(all_turns) == 3
    # Oldest two evicted; turn_ids 3, 4, 5 remain.
    assert [t.turn_id for t in all_turns] == [3, 4, 5]
    # get_by_id for an evicted turn returns None.
    assert store.get_by_id(1) is None


def test_tool_action_is_immutable_record():
    a = ToolAction(name="Read", args_preview="foo.py", ok=True, duration_s=0.05)
    with pytest.raises(Exception):
        a.name = "Edit"  # type: ignore[misc]


def test_reasoning_turn_records_action_count():
    actions = [
        ToolAction(name="Read", args_preview="a.py", ok=True, duration_s=0.1),
        ToolAction(name="Edit", args_preview="b.py", ok=True, duration_s=0.2),
    ]
    store = ReasoningStore()
    turn = store.append(thinking="reasoning", duration_s=1.0, tool_actions=actions)
    assert turn.action_count == 2
    assert turn.tool_actions[0].name == "Read"


def test_empty_thinking_still_records_turn():
    """Tool-only turns (no extended-thinking) must still be queryable."""
    store = ReasoningStore()
    turn = store.append(
        thinking="",
        duration_s=0.5,
        tool_actions=[
            ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.05),
        ],
    )
    assert turn.turn_id == 1
    assert turn.thinking == ""
    assert turn.action_count == 1


def test_peek_next_id_returns_id_of_next_append():
    store = ReasoningStore()
    assert store.peek_next_id() == 1
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    assert store.peek_next_id() == 2


def test_reasoning_turn_dataclass_is_frozen():
    """Frozen contract: a recorded turn must not be mutated by callers
    so /reasoning show always shows the same content as the original
    push."""
    store = ReasoningStore()
    t = store.append(thinking="x", duration_s=0.1, tool_actions=[])
    assert isinstance(t, ReasoningTurn)
    with pytest.raises(Exception):
        t.thinking = "tampered"  # type: ignore[misc]


# ─── render_turn_tree (Task 5) ──────────────────────────────────────────


def test_render_turn_tree_returns_rich_tree_with_expected_nodes():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import render_turn_tree

    store = ReasoningStore()
    turn = store.append(
        thinking="Let me think about how to do this carefully.",
        duration_s=0.8,
        tool_actions=[
            ToolAction(name="Read", args_preview="foo.py", ok=True, duration_s=0.05),
            ToolAction(name="Edit", args_preview="bar.py", ok=True, duration_s=0.12),
            ToolAction(name="Bash", args_preview="ls", ok=False, duration_s=0.03),
        ],
    )

    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()

    assert "Turn #1" in text
    assert "Thought for" in text
    assert "3 actions" in text
    assert "Let me think about" in text
    assert "Read" in text and "foo.py" in text
    assert "Edit" in text and "bar.py" in text
    assert "Bash" in text and "ls" in text
    # Failed call indicator.
    assert "✗" in text


def test_render_turn_tree_handles_no_thinking():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import render_turn_tree

    store = ReasoningStore()
    turn = store.append(
        thinking="",
        duration_s=0.2,
        tool_actions=[
            ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.05),
        ],
    )
    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()
    assert "Turn #1" in text
    assert "Bash" in text
    # No reasoning child node when thinking is empty.
    assert "Reasoning:" not in text
    assert "(no extended thinking)" in text


def test_render_turn_tree_handles_no_actions():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import render_turn_tree

    store = ReasoningStore()
    turn = store.append(thinking="just thinking", duration_s=0.5, tool_actions=[])
    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()
    assert "Turn #1" in text
    assert "just thinking" in text
    assert "(no tool actions)" in text


def test_render_turns_to_text_emits_no_ansi():
    """Critical for SlashCommandResult.output: must not contain ANSI
    escape codes since the dispatcher routes the output as message
    content (see opencomputer/agent/loop.py)."""
    from opencomputer.cli_ui.reasoning_store import render_turns_to_text

    store = ReasoningStore()
    store.append(thinking="alpha", duration_s=0.1, tool_actions=[
        ToolAction(name="Read", args_preview="x.py", ok=True, duration_s=0.05),
    ])
    text = render_turns_to_text(store.get_all())
    # ANSI escape sequences begin with \x1b[
    assert "\x1b[" not in text, f"unexpected ANSI in output: {text!r}"
    # But Unicode tree connectors must be preserved.
    assert "Turn #1" in text
    assert ("├──" in text or "└──" in text)


def test_render_turns_to_text_handles_multiple_turns():
    from opencomputer.cli_ui.reasoning_store import render_turns_to_text

    store = ReasoningStore()
    store.append(thinking="alpha", duration_s=0.1, tool_actions=[])
    store.append(thinking="beta", duration_s=0.2, tool_actions=[])
    text = render_turns_to_text(store.get_all())
    assert "Turn #1" in text
    assert "Turn #2" in text
    assert "alpha" in text
    assert "beta" in text


def test_render_turns_to_text_handles_empty_list():
    """Defensive: an empty list returns an empty string, not a crash."""
    from opencomputer.cli_ui.reasoning_store import render_turns_to_text

    assert render_turns_to_text([]) == ""


# ─── Summary support (v2 — Thinking History UI) ──────────────────────────


def test_reasoning_turn_has_optional_summary_field():
    """Summary defaults to None; new field doesn't break existing
    construction."""
    turn = ReasoningTurn(turn_id=1, thinking="x", duration_s=0.1)
    assert turn.summary is None


def test_store_update_summary_sets_field_for_existing_turn():
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    store.append(thinking="y", duration_s=0.2, tool_actions=[])
    store.update_summary(turn_id=1, summary="first turn")
    store.update_summary(turn_id=2, summary="second turn")
    t1 = store.get_by_id(1)
    t2 = store.get_by_id(2)
    assert t1 is not None and t1.summary == "first turn"
    assert t2 is not None and t2.summary == "second turn"


def test_store_update_summary_unknown_id_is_noop():
    """No exception when the turn was evicted or never existed —
    summary writes are best-effort from a background thread."""
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    # Should not raise.
    store.update_summary(turn_id=99, summary="never landed")
    t = store.get_by_id(1)
    assert t is not None and t.summary is None


def test_render_turn_tree_includes_summary_when_present():
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import render_turn_tree

    store = ReasoningStore()
    store.append(thinking="raw thinking text", duration_s=0.5, tool_actions=[])
    store.update_summary(turn_id=1, summary="Wrote a poem about sloths")
    turn = store.get_latest()
    assert turn is not None
    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()
    assert "Wrote a poem about sloths" in text
    assert "Turn #1" in text


def test_render_turn_tree_omits_summary_line_when_none():
    """No summary → header is just the metadata line."""
    import io

    from rich.console import Console

    from opencomputer.cli_ui.reasoning_store import render_turn_tree

    store = ReasoningStore()
    store.append(thinking="raw", duration_s=0.5, tool_actions=[])
    turn = store.get_latest()
    assert turn is not None
    assert turn.summary is None
    tree = render_turn_tree(turn)
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(tree)
    text = out.getvalue()
    assert "Turn #1" in text
    assert "Thought for" in text
