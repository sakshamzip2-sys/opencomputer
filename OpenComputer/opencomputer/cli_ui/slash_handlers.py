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
    """Everything a slash handler might need from the chat loop.

    The ``on_*`` callbacks delegate state mutations the chat loop owns
    (e.g. rebinding ``nonlocal session_id`` for ``/clear`` or
    ``/resume``). Default no-op callables keep the dataclass usable in
    test contexts that don't exercise those handlers.
    """

    console: Console
    session_id: str
    config: Any  # Config — typed loosely to avoid import cycle
    on_clear: Callable[[], None]
    get_cost_summary: Callable[[], dict[str, int]]
    get_session_list: Callable[[], list[dict[str, Any]]]
    #: ``/rename <title>`` — returns True on success, False if the title
    #: couldn't be persisted (no current session, DB error).
    on_rename: Callable[[str], bool] = lambda title: False
    #: ``/resume [last|<id-prefix>|pick]`` — returns True if the chat
    #: loop swapped to the target session; False on no-match / ambiguous
    #: prefix / DB error.
    on_resume: Callable[[str], bool] = lambda target: False
    #: ``/snapshot create [<label>]`` — archive critical state files;
    #: returns the new snapshot id, or ``None`` if no eligible files.
    on_snapshot_create: Callable[[str | None], str | None] = lambda label: None
    #: ``/snapshot list`` — return snapshot manifests, newest first.
    on_snapshot_list: Callable[[], list[dict]] = list
    #: ``/snapshot restore <id>`` — overwrite current state from snapshot;
    #: returns count of files restored (0 if id not found).
    on_snapshot_restore: Callable[[str], int] = lambda sid: 0
    #: ``/snapshot prune`` — drop snapshots beyond the keep cap; returns
    #: count deleted.
    on_snapshot_prune: Callable[[], int] = lambda: 0


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


def _handle_rename(ctx: SlashContext, args: list[str]) -> SlashResult:
    title = " ".join(args).strip()
    if not title:
        ctx.console.print(
            "[red]/rename needs a title[/red] — e.g. `/rename my-debug-session`"
        )
        return SlashResult(handled=True)
    ok = ctx.on_rename(title)
    if ok:
        ctx.console.print(f"[green]session renamed →[/green] {title}")
    else:
        ctx.console.print("[red]rename failed[/red] (no current session?)")
    return SlashResult(handled=True)


def _handle_resume(ctx: SlashContext, args: list[str]) -> SlashResult:
    target = (args[0] if args else "pick").strip()
    ok = ctx.on_resume(target)
    if not ok:
        ctx.console.print(
            "[red]resume failed[/red] — target not found, ambiguous prefix, "
            "or no prior sessions"
        )
    return SlashResult(handled=True)


def _handle_snapshot(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/snapshot [create [<label>]|list|restore <id>|prune]``.

    Default subcommand (no args) is ``list`` — show recent snapshots.
    """
    sub = (args[0].lower() if args else "list").strip()
    if sub == "create":
        label = " ".join(args[1:]).strip() or None
        sid = ctx.on_snapshot_create(label)
        if sid:
            ctx.console.print(f"[green]snapshot created:[/green] {sid}")
        else:
            ctx.console.print(
                "[yellow]snapshot empty[/yellow] — no eligible state files found "
                "(profile_home may be uninitialized)."
            )
        return SlashResult(handled=True)

    if sub == "list":
        items = ctx.on_snapshot_list()
        if not items:
            ctx.console.print("[dim]no snapshots.[/dim]")
            return SlashResult(handled=True)
        ctx.console.print(f"[bold]snapshots ({len(items)}):[/bold]")
        for i, m in enumerate(items, start=1):
            sid = m.get("id", "?")
            n = m.get("file_count", 0)
            sz = m.get("total_size", 0)
            label = m.get("label") or ""
            label_part = f"  [cyan]{label}[/cyan]" if label else ""
            ctx.console.print(
                f"  [dim]{i}.[/dim] {sid}{label_part}  "
                f"[dim]({n} files, {sz} bytes)[/dim]"
            )
        return SlashResult(handled=True)

    if sub == "restore":
        if len(args) < 2:
            ctx.console.print(
                "[red]usage:[/red] /snapshot restore <id>  "
                "[dim](try /snapshot list first)[/dim]"
            )
            return SlashResult(handled=True)
        sid = args[1].strip()
        n = ctx.on_snapshot_restore(sid)
        if n > 0:
            ctx.console.print(
                f"[green]restored {n} files[/green] from snapshot {sid}.\n"
                "[yellow]restart recommended[/yellow] for state.db / config "
                "changes to take effect."
            )
        else:
            ctx.console.print(
                f"[red]restore failed[/red] — snapshot {sid!r} not found "
                "or has no manifest."
            )
        return SlashResult(handled=True)

    if sub == "prune":
        n = ctx.on_snapshot_prune()
        ctx.console.print(f"[green]pruned[/green] — {n} snapshot(s) deleted.")
        return SlashResult(handled=True)

    ctx.console.print(
        f"[red]unknown subcommand:[/red] /snapshot {sub}  "
        "[dim](try create | list | restore <id> | prune)[/dim]"
    )
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
    "rename": _handle_rename,
    "resume": _handle_resume,
    "snapshot": _handle_snapshot,
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
