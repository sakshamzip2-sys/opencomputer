"""Behavioral tests for /reasoning show — retroactive expand."""
from __future__ import annotations

import asyncio

from opencomputer.agent.slash_commands_impl.reasoning_cmd import ReasoningCommand
from opencomputer.cli_ui.reasoning_store import ReasoningStore, ToolAction
from plugin_sdk.runtime_context import RuntimeContext


def _runtime_with_store(store: ReasoningStore | None) -> RuntimeContext:
    rt = RuntimeContext()
    if store is not None:
        rt.custom["_reasoning_store"] = store
    return rt


def _run(cmd: ReasoningCommand, args: str, runtime: RuntimeContext):
    return asyncio.run(cmd.execute(args, runtime))


def test_show_with_no_store_returns_helpful_message():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    res = _run(cmd, "show", rt)
    assert res.handled
    assert (
        "no reasoning history" in res.output.lower()
        or "not available" in res.output.lower()
    )


def test_show_when_store_empty_returns_helpful_message():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(ReasoningStore())
    res = _run(cmd, "show", rt)
    assert res.handled
    assert "no" in res.output.lower()


def test_show_renders_latest_turn_as_tree():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(
        thinking="first turn thinking",
        duration_s=0.5,
        tool_actions=[
            ToolAction(name="Read", args_preview="a.py", ok=True, duration_s=0.1)
        ],
    )
    store.append(
        thinking="second turn thinking",
        duration_s=0.7,
        tool_actions=[
            ToolAction(name="Edit", args_preview="b.py", ok=True, duration_s=0.2)
        ],
    )
    rt = _runtime_with_store(store)
    res = _run(cmd, "show", rt)
    assert res.handled
    out = res.output
    assert "Turn #2" in out
    assert "second turn thinking" in out
    assert "Edit" in out
    # Latest only — first turn must not appear.
    assert "Turn #1" not in out


def test_show_last_is_alias_for_show():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="t", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show last", rt)
    assert res.handled
    assert "Turn #1" in res.output


def test_show_specific_turn_by_id():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="alpha", duration_s=0.1, tool_actions=[])
    store.append(thinking="beta", duration_s=0.1, tool_actions=[])
    store.append(thinking="gamma", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show 2", rt)
    assert res.handled
    assert "Turn #2" in res.output
    assert "beta" in res.output
    assert "alpha" not in res.output
    assert "gamma" not in res.output


def test_show_unknown_turn_id_returns_error():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show 99", rt)
    assert res.handled
    assert "no turn" in res.output.lower() or "unknown" in res.output.lower()


def test_show_all_renders_every_turn():
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="alpha", duration_s=0.1, tool_actions=[])
    store.append(thinking="beta", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res = _run(cmd, "show all", rt)
    assert res.handled
    assert "Turn #1" in res.output
    assert "Turn #2" in res.output
    assert "alpha" in res.output
    assert "beta" in res.output


def test_legacy_show_still_sets_flag_for_next_turn():
    """Backwards compat: existing reasoning persistence behavior expects
    show to also flip the flag so streaming providers expose the
    raw <think> text on the NEXT turn. Both behaviors must coexist.
    """
    cmd = ReasoningCommand()
    rt = _runtime_with_store(ReasoningStore())
    _ = _run(cmd, "show", rt)
    assert rt.custom.get("show_reasoning") is True


def test_hide_still_clears_flag():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    rt.custom["show_reasoning"] = True
    res = _run(cmd, "hide", rt)
    assert res.handled
    assert rt.custom.get("show_reasoning") is False


def test_status_unchanged():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    res = _run(cmd, "status", rt)
    assert res.handled
    assert "effort=" in res.output


def test_level_setting_unchanged():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    res = _run(cmd, "high", rt)
    assert res.handled
    assert "high" in res.output
    assert rt.custom.get("reasoning_effort") == "high"


def test_show_zero_or_negative_id_returns_no_turn():
    """Defensive: only positive ints are valid turn ids."""
    cmd = ReasoningCommand()
    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.1, tool_actions=[])
    rt = _runtime_with_store(store)
    res_zero = _run(cmd, "show 0", rt)
    assert res_zero.handled
    assert "no turn" in res_zero.output.lower() or "unknown" in res_zero.output.lower()


def test_unknown_subcommand_returns_usage():
    cmd = ReasoningCommand()
    rt = _runtime_with_store(None)
    res = _run(cmd, "garbage", rt)
    assert res.handled
    assert "Usage:" in res.output
