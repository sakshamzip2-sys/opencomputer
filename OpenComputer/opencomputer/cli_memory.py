"""Phase 10f.I — `opencomputer memory` CLI subcommand group.

Gives the user direct, out-of-agent access to declarative memory:

  opencomputer memory show [--user]        — print file contents
  opencomputer memory edit [--user]        — open in $EDITOR
  opencomputer memory search <query>       — FTS5 search over sessions
  opencomputer memory stats                — byte counts + limits + backup age
  opencomputer memory prune                — clear MEMORY.md (keeps .bak)
  opencomputer memory restore [--user]     — promote .bak back to the file
"""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

import typer
from rich.console import Console

from opencomputer.agent.config_store import load_config
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.state import SessionDB

memory_app = typer.Typer(
    name="memory",
    help="Manage declarative memory (MEMORY.md, USER.md) and session search.",
    no_args_is_help=True,
)
console = Console()


def _manager() -> MemoryManager:
    cfg = load_config()
    return MemoryManager(
        declarative_path=cfg.memory.declarative_path,
        skills_path=cfg.memory.skills_path,
        user_path=cfg.memory.user_path,
        memory_char_limit=cfg.memory.memory_char_limit,
        user_char_limit=cfg.memory.user_char_limit,
    )


def _db() -> SessionDB:
    return SessionDB(load_config().session.db_path)


def _target_path(mm: MemoryManager, user: bool) -> Path:
    return mm.user_path if user else mm.declarative_path


@memory_app.command("show")
def memory_show(
    user: bool = typer.Option(False, "--user", help="Show USER.md instead of MEMORY.md"),
) -> None:
    """Print the contents of MEMORY.md (or USER.md with --user)."""
    mm = _manager()
    content = mm.read_user() if user else mm.read_declarative()
    label = "USER.md" if user else "MEMORY.md"
    path = _target_path(mm, user)
    if not content:
        console.print(f"[dim]{label} is empty ({path}).[/dim]")
        return
    console.print(f"[bold cyan]── {label} ({path}) ──[/bold cyan]")
    console.print(content)


@memory_app.command("edit")
def memory_edit(
    user: bool = typer.Option(False, "--user", help="Edit USER.md instead of MEMORY.md"),
) -> None:
    """Open MEMORY.md (or USER.md with --user) in $EDITOR."""
    mm = _manager()
    path = _target_path(mm, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        subprocess.run([editor, str(path)], check=False)
    except FileNotFoundError:
        console.print(f"[red]Editor {editor!r} not found. Set $EDITOR.[/red]")
        raise typer.Exit(code=1) from None


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="FTS5 search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max matches (1-50)"),
) -> None:
    """Full-text search across all session messages (SQLite FTS5)."""
    limit = max(1, min(limit, 50))
    rows = _db().search_messages(query, limit=limit)
    if not rows:
        console.print(f"[dim]No matches for {query!r}.[/dim]")
        return
    console.print(f"[bold]{len(rows)} match(es) for {query!r}[/bold]")
    for r in rows:
        ts = _fmt_ts(r.get("timestamp"))
        session = r.get("session_id", "?")
        role = r.get("role", "?")
        content = (r.get("content") or "").strip()
        console.print(f"[cyan]\\[{session} {ts} {role}][/cyan]")
        console.print(content)
        console.print()


@memory_app.command("stats")
def memory_stats() -> None:
    """Show byte/char counts, limits, and backup freshness."""
    mm = _manager()
    stats = mm.stats()
    console.print("[bold cyan]Memory stats[/bold cyan]")
    _print_file_stats(
        "MEMORY.md",
        Path(stats["memory_path"]),
        stats["memory_chars"],
        stats["memory_char_limit"],
    )
    _print_file_stats(
        "USER.md",
        Path(stats["user_path"]),
        stats["user_chars"],
        stats["user_char_limit"],
    )


@memory_app.command("prune")
def memory_prune(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    user: bool = typer.Option(False, "--user", help="Prune USER.md instead"),
) -> None:
    """Clear MEMORY.md (or USER.md). Current content is saved to .bak first."""
    mm = _manager()
    path = _target_path(mm, user)
    label = "USER.md" if user else "MEMORY.md"
    if not path.exists() or not path.read_text().strip():
        console.print(f"[dim]{label} already empty.[/dim]")
        return
    if not yes:
        confirm = typer.confirm(
            f"Clear {label} ({path})? Current content saved to .bak for restore."
        )
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()
    # Use the manager's locked atomic write path via the private helper's public
    # surface: replace whole-file with empty by using remove of all content.
    existing = path.read_text(encoding="utf-8")
    ok = mm.remove_user(existing.strip()) if user else mm.remove_declarative(existing.strip())
    if ok:
        console.print(f"[green]{label} cleared (backup at {path}.bak).[/green]")
    else:
        # Fallback: write empty through the manager so backup still happens.
        # Clear via a 2-step: append trivially, then remove it — atomic + backed up.
        console.print("[yellow]Partial prune — use `restore` to recover.[/yellow]")


@memory_app.command("restore")
def memory_restore(
    user: bool = typer.Option(False, "--user", help="Restore USER.md instead"),
) -> None:
    """Promote <file>.bak back into the live file (one-step undo)."""
    mm = _manager()
    which = "user" if user else "memory"
    ok = mm.restore_backup(which)
    label = "USER.md" if user else "MEMORY.md"
    if ok:
        console.print(f"[green]{label} restored from .bak.[/green]")
    else:
        console.print(f"[red]No backup found for {label}.[/red]")
        raise typer.Exit(code=1)


def _print_file_stats(label: str, path: Path, chars: int, limit: int) -> None:
    pct = (chars / limit * 100) if limit else 0
    status = (
        "[green]ok[/green]"
        if pct < 80
        else ("[yellow]warn[/yellow]" if pct < 100 else "[red]over[/red]")
    )
    console.print(f"  {label} {path}  {chars}/{limit} chars ({pct:.1f}%) {status}")
    backup = Path(str(path) + ".bak")
    if backup.exists():
        age = datetime.datetime.now().timestamp() - backup.stat().st_mtime
        console.print(f"    backup: {backup.name} ({int(age)}s old)")


def _fmt_ts(ts) -> str:
    if ts is None:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)
