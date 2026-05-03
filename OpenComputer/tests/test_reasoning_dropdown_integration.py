"""End-to-end: a chat turn with thinking + tools makes /reasoning show work."""
from __future__ import annotations

import asyncio
import io

from rich.console import Console

from opencomputer.agent.slash_commands_impl.reasoning_cmd import ReasoningCommand
from opencomputer.cli_ui import ReasoningStore, StreamingRenderer
from plugin_sdk.runtime_context import RuntimeContext


def test_full_loop_session_store_renderer_then_show():
    """Simulates the cli.py wiring: one ReasoningStore for the whole
    session, renderer pushes per turn, /reasoning show retrieves."""
    runtime = RuntimeContext()
    runtime.custom["_reasoning_store"] = ReasoningStore()
    store = runtime.custom["_reasoning_store"]

    # Turn 1
    with StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store) as r:
        r.on_thinking_chunk("turn one reasoning")
        idx = r.on_tool_start("Read", "a.py")
        r.on_tool_end("Read", idx, ok=True)
        r.finalize(
            reasoning="turn one reasoning",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.3,
            show_reasoning=False,
        )

    # Turn 2 — fresh renderer, same store
    with StreamingRenderer(Console(file=io.StringIO()), reasoning_store=store) as r:
        r.on_thinking_chunk("turn two reasoning")
        r.finalize(
            reasoning="turn two reasoning",
            iterations=1,
            in_tok=1,
            out_tok=1,
            elapsed_s=0.4,
            show_reasoning=False,
        )

    # /reasoning show retrieves turn 2.
    cmd = ReasoningCommand()
    res = asyncio.run(cmd.execute("show", runtime))
    assert "Turn #2" in res.output
    assert "turn two reasoning" in res.output

    # /reasoning show 1 retrieves turn 1.
    res = asyncio.run(cmd.execute("show 1", runtime))
    assert "Turn #1" in res.output
    assert "turn one reasoning" in res.output
    assert "Read" in res.output

    # /reasoning show all retrieves both.
    res = asyncio.run(cmd.execute("show all", runtime))
    assert "Turn #1" in res.output
    assert "Turn #2" in res.output


def test_cli_ui_package_re_exports_reasoning_store():
    """Public-API contract: /reasoning show and CLI wiring import these
    from the package, not the deep module path."""
    from opencomputer.cli_ui import (
        ReasoningStore,
        ReasoningTurn,
        ToolAction,
        render_turn_tree,
        render_turns_to_text,
    )

    assert ReasoningStore is not None
    assert ReasoningTurn is not None
    assert ToolAction is not None
    assert callable(render_turn_tree)
    assert callable(render_turns_to_text)
