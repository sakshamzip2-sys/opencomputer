"""Concrete handlers for the slash commands defined in :mod:`slash`.

Each handler takes a :class:`SlashContext` (the chat loop wires it up
once at session start) and returns a :class:`SlashResult`. Handlers are
intentionally small — anything that needs Rich rendering or filesystem
access uses ``ctx.console``; anything that needs agent state goes
through the callbacks (``on_clear``, ``get_cost_summary``, etc.).

The layer of indirection through callbacks (rather than passing the
agent loop / config directly) keeps this module testable without
booting an agent.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from opencomputer.cli_ui.slash import (
    SLASH_REGISTRY,
    CommandDef,
    SlashResult,
    is_slash_command,
    resolve_command,
)


@dataclass
class SlashContext:
    """Everything a slash handler might need from the chat loop."""

    console: Console
    session_id: str
    config: Any  # Config — typed loosely to avoid import cycle
    on_clear: Callable[[], None]
    get_cost_summary: Callable[[], dict[str, int]]
    get_session_list: Callable[[], list[dict[str, Any]]]


def _split_args(text: str) -> tuple[str, list[str]]:
    """Split ``/cmd arg1 arg2`` into ``("cmd", ["arg1", "arg2"])``."""
    parts = text.lstrip("/").split()
    if not parts:
        return ("", [])
    return (parts[0], parts[1:])


def _handle_exit(ctx: SlashContext, args: list[str]) -> SlashResult:
    return SlashResult(handled=True, exit_loop=True, message="bye.")


def _handle_clear(ctx: SlashContext, args: list[str]) -> SlashResult:
    ctx.on_clear()
    ctx.console.print("[dim]session cleared.[/dim]")
    return SlashResult(handled=True)


def _handle_help(ctx: SlashContext, args: list[str]) -> SlashResult:
    table = Table(title="Slash commands", show_header=True, header_style="bold")
    table.add_column("Command", style="cyan")
    table.add_column("Aliases", style="dim")
    table.add_column("Description")
    for cmd in SLASH_REGISTRY:
        aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else ""
        ctx_name = f"/{cmd.name}"
        if cmd.args_hint:
            ctx_name = f"{ctx_name} {cmd.args_hint}"
        table.add_row(ctx_name, aliases, cmd.description)
    ctx.console.print(table)
    return SlashResult(handled=True)


def _handle_screenshot(ctx: SlashContext, args: list[str]) -> SlashResult:
    """Dump the rendered console to a file. Format inferred from extension:
    ``.svg`` → SVG, ``.html`` → HTML, anything else → text."""
    if args:
        path = Path(args[0]).expanduser().resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path.cwd() / f"oc-screenshot-{ts}.txt"
    suffix = path.suffix.lower()
    if suffix == ".svg":
        ctx.console.save_svg(str(path), title="OpenComputer")
    elif suffix in (".html", ".htm"):
        ctx.console.save_html(str(path))
    else:
        ctx.console.save_text(str(path))
    ctx.console.print(f"[green]screenshot →[/green] {path}")
    return SlashResult(handled=True)


def _handle_export(ctx: SlashContext, args: list[str]) -> SlashResult:
    """Same as screenshot but defaults to .md and uses save_text."""
    if args:
        path = Path(args[0]).expanduser().resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path.cwd() / f"oc-transcript-{ts}.md"
    ctx.console.save_text(str(path))
    ctx.console.print(f"[green]transcript →[/green] {path}")
    return SlashResult(handled=True)


def _handle_cost(ctx: SlashContext, args: list[str]) -> SlashResult:
    summary = ctx.get_cost_summary()
    in_tok = summary.get("in", 0)
    out_tok = summary.get("out", 0)
    ctx.console.print(
        f"[bold]session tokens[/bold]  in={in_tok}  out={out_tok}  total={in_tok + out_tok}"
    )
    return SlashResult(handled=True)


def _handle_model(ctx: SlashContext, args: list[str]) -> SlashResult:
    if not args:
        m = getattr(ctx.config.model, "model", "?")
        p = getattr(ctx.config.model, "provider", "?")
        ctx.console.print(f"[bold]active model[/bold]  {m}  ({p})")
        return SlashResult(handled=True)
    # Switching mid-session is intentionally not implemented in Phase 1.
    ctx.console.print(
        "[yellow]switching mid-session not implemented yet — restart with --model[/yellow]"
    )
    return SlashResult(handled=True)


def _handle_sessions(ctx: SlashContext, args: list[str]) -> SlashResult:
    sessions = ctx.get_session_list()
    if not sessions:
        ctx.console.print("[dim]no prior sessions.[/dim]")
        return SlashResult(handled=True)
    table = Table(title="Recent sessions", show_header=True)
    table.add_column("id", style="cyan")
    table.add_column("started_at")
    for s in sessions[:20]:
        table.add_row(s.get("id", "?"), str(s.get("started_at", "?")))
    ctx.console.print(table)
    return SlashResult(handled=True)


_HANDLERS: dict[str, Callable[[SlashContext, list[str]], SlashResult]] = {
    "exit": _handle_exit,
    "clear": _handle_clear,
    "help": _handle_help,
    "screenshot": _handle_screenshot,
    "export": _handle_export,
    "cost": _handle_cost,
    "model": _handle_model,
    "sessions": _handle_sessions,
}


def dispatch_slash(text: str, ctx: SlashContext) -> SlashResult:
    """Dispatch a slash-command string to its handler.

    Returns ``SlashResult(handled=False)`` for non-slash text so the
    caller can fall back to "treat as normal message". Unknown slash
    commands are consumed (handled=True) with an error message — we
    don't want them to leak to the LLM.
    """
    if not is_slash_command(text):
        return SlashResult(handled=False)
    name, args = _split_args(text)
    cmd: CommandDef | None = resolve_command(name)
    if cmd is None:
        ctx.console.print(f"[red]unknown command:[/red] /{name}  (try /help)")
        return SlashResult(handled=True)
    handler = _HANDLERS[cmd.name]
    return handler(ctx, args)
