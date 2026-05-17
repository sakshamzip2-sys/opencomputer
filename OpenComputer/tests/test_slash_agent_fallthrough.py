"""The `oc chat` REPL routes unknown-to-cli_ui slashes to the agent registry.

`oc chat` historically dispatched ONLY the cli_ui slash registry
(`cli_ui/slash.py`), so the ~40 agent `SlashCommand`s in
`agent/slash_commands_impl/` — `/copy`, `/rollback`, `/background`,
`/agents`, `/btw`, `/save`, `/branch`, … — were reachable on
gateway/wire/ACP but produced "unknown command" in the local chat REPL.

`dispatch_slash` now takes an optional `on_unknown` hook. cli.py wires
it to `try_dispatch_agent_slash`, which dispatches via the agent
registry — so every agent slash command works in `oc chat`, generalising
the per-command `/reasoning` / `/sources` bridges.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from rich.console import Console

from opencomputer.agent.slash_commands import try_dispatch_agent_slash
from opencomputer.cli_ui.slash import SlashResult
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    dispatch_agent_slash_to_console,
    dispatch_slash,
)
from plugin_sdk.runtime_context import RuntimeContext


def _ctx(console: Console | None = None) -> SlashContext:
    return SlashContext(
        console=console or Console(record=True),
        session_id="s1",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=list,
    )


# ---- dispatch_slash on_unknown hook ----


def test_on_unknown_fires_for_non_cli_ui_command():
    """A slash not in the cli_ui registry routes to the on_unknown hook."""
    seen: list[str] = []
    ctx = _ctx()

    result = dispatch_slash(
        "/copy hello",
        ctx,
        on_unknown=lambda t: seen.append(t) or SlashResult(handled=True),
    )

    assert seen == ["/copy hello"]
    assert result.handled is True


def test_on_unknown_does_not_fire_for_known_cli_ui_command():
    """A real cli_ui command (/help) is handled normally — the hook is untouched."""
    seen: list[str] = []
    ctx = _ctx()

    dispatch_slash(
        "/help",
        ctx,
        on_unknown=lambda t: seen.append(t) or SlashResult(handled=True),
    )

    assert seen == []


def test_unknown_without_hook_still_prints_error():
    """Back-compat: with no on_unknown hook, the old 'unknown command' path holds."""
    console = Console(record=True)
    ctx = _ctx(console)

    result = dispatch_slash("/totally-bogus", ctx)

    assert result.handled is True
    assert "unknown" in console.export_text().lower()


# ---- agent-registry dispatch ----


def test_try_dispatch_agent_slash_resolves_an_agent_command():
    """/copy — an agent SlashCommand absent from the cli_ui registry — dispatches."""
    result = try_dispatch_agent_slash("/copy hello world", RuntimeContext(custom={}))

    assert result is not None
    assert result.output  # CopyCommand produced a confirmation message


def test_try_dispatch_agent_slash_returns_none_for_unknown():
    """A command in neither registry → None, so the caller can show 'unknown'."""
    assert try_dispatch_agent_slash("/zzz-not-a-command", RuntimeContext(custom={})) is None


# ---- the fallthrough renderer (the cli.py closure's tested core) ----


def test_dispatch_agent_slash_to_console_renders_a_command():
    """An agent command's output is printed; the result is handled."""
    console = Console(record=True)
    result = dispatch_agent_slash_to_console(
        "/copy hi", RuntimeContext(custom={}), console
    )
    assert result.handled is True
    out = console.export_text().lower()
    assert "copied" in out or "clipboard" in out


def test_dispatch_agent_slash_to_console_prints_unknown_for_no_match():
    """A slash in neither registry → an 'unknown command' line, still handled."""
    console = Console(record=True)
    result = dispatch_agent_slash_to_console(
        "/zzz-bogus", RuntimeContext(custom={}), console
    )
    assert result.handled is True
    assert "unknown command" in console.export_text().lower()
