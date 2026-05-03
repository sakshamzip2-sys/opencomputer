"""Tests for ``/reasoning`` user-typed slash dispatch (2026-05-03).

Before this fix, ``/reasoning`` only existed in the agent-side slash
registry (``opencomputer.agent.slash_commands``). User-typed input
goes through ``cli_ui.slash_handlers.dispatch_slash`` which only
consults the cli_ui registry, so ``/reasoning`` returned
``unknown command: /reasoning`` despite the agent-side ReasoningCommand
being fully implemented.

This test pins the resolved-and-handled contract: ``/reasoning`` is in
the registry, dispatches via ``on_reasoning_dispatch``, and prints the
callback's output to the console.
"""
from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

from rich.console import Console

from opencomputer.cli_ui.slash import is_slash_command, resolve_command
from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash


def _ctx(reasoning_cb) -> SlashContext:
    buf = StringIO()
    return SlashContext(
        console=Console(file=buf, force_terminal=False, width=120),
        session_id="s-test",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"total_tokens": 0},
        get_session_list=list,
        on_reasoning_dispatch=reasoning_cb,
    )


def _output(ctx: SlashContext) -> str:
    return ctx.console.file.getvalue()


def test_reasoning_in_slash_registry() -> None:
    assert resolve_command("reasoning") is not None
    assert resolve_command("/reasoning") is not None


def test_reasoning_recognised_as_slash_command() -> None:
    assert is_slash_command("/reasoning")
    assert is_slash_command("/reasoning show")


def test_reasoning_no_args_dispatches_with_empty_string() -> None:
    captured: list[str] = []

    def cb(args: str) -> str:
        captured.append(args)
        return "reasoning: effort=medium, display=hidden"

    ctx = _ctx(cb)
    result = dispatch_slash("/reasoning", ctx)

    assert result.handled is True
    assert captured == [""]
    assert "reasoning: effort=medium" in _output(ctx)


def test_reasoning_with_args_passes_them_through() -> None:
    captured: list[str] = []

    def cb(args: str) -> str:
        captured.append(args)
        return "ok"

    ctx = _ctx(cb)
    dispatch_slash("/reasoning show 5", ctx)

    assert captured == ["show 5"]


def test_reasoning_callback_exception_is_caught() -> None:
    def cb(_args: str) -> str:
        raise RuntimeError("boom")

    ctx = _ctx(cb)
    result = dispatch_slash("/reasoning", ctx)

    assert result.handled is True
    out = _output(ctx)
    assert "/reasoning failed" in out
    assert "boom" in out


def test_reasoning_no_longer_returns_unknown_command() -> None:
    """Regression guard: prior to this fix, /reasoning hit the
    ``unknown command: /reasoning`` branch in dispatch_slash."""
    ctx = _ctx(lambda _args: "status")
    dispatch_slash("/reasoning", ctx)
    assert "unknown command" not in _output(ctx)
