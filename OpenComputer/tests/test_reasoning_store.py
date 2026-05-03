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
