"""`opencomputer session` CLI subcommand — list, show, fork, resume.

Sub-project G.33 (Tier 4). Surfaces the existing ``SessionDB``
session/message storage to the user via four subcommands:

* ``list``    — table of recent sessions (id, started, platform, msgs).
* ``show ID`` — print one session's metadata + a head-of-messages
                preview without dumping the entire history.
* ``fork ID`` — clone a session's history into a NEW session id so
                you can branch the conversation without polluting the
                source. Useful for "what would have happened if I'd
                asked Y instead of X?".
* ``resume ID`` — print the exact ``opencomputer chat --resume <id>``
                command to drop into the existing session. We don't
                spawn the chat ourselves because typer-inside-typer is
                fiddly; the user copy-pastes the line.

Storage is the per-profile ``sessions.db`` resolved via
``opencomputer.agent.config._home()``. The CLI never touches profile
state directly — it just reads/writes through ``SessionDB``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import _home
from opencomputer.agent.state import SessionDB

session_app = typer.Typer(
    name="session", help="Inspect, fork, and resume saved sessions."
)
console = Console()


def _db() -> SessionDB:
    """Construct a SessionDB rooted at the active profile."""
    db_path: Path = _home() / "sessions.db"
    return SessionDB(db_path)


def _format_started(started_at: float | int | None) -> str:
    """Render the started_at unix timestamp as YYYY-MM-DD HH:MM."""
    if not started_at:
        return "-"
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(started_at)).strftime(
        "%Y-%m-%d %H:%M"
    )


@session_app.command("list")
def session_list(
    limit: int = typer.Option(
        20, "--limit", "-n", help="Max sessions to show (1-200).", min=1, max=200,
    ),
) -> None:
    """List recent sessions in the active profile."""
    rows = _db().list_sessions(limit=limit)
    if not rows:
        console.print("[dim]no sessions yet — run `opencomputer chat` first.[/dim]")
        return
    t = Table(show_lines=False)
    t.add_column("id", style="cyan", overflow="fold")
    t.add_column("started", style="dim")
    t.add_column("platform")
    t.add_column("model", style="dim")
    t.add_column("msgs", justify="right")
    t.add_column("title")
    for r in rows:
        t.add_row(
            r.get("id", ""),
            _format_started(r.get("started_at")),
            r.get("platform", "") or "",
            (r.get("model", "") or "")[:40],
            str(r.get("message_count", 0)),
            (r.get("title", "") or "")[:60],
        )
    console.print(t)


@session_app.command("show")
def session_show(
    session_id: str = typer.Argument(..., help="Session id (full UUID hex)."),
    head: int = typer.Option(
        5,
        "--head",
        help="Show the first N messages (default 5; 0 = metadata only).",
        min=0,
        max=200,
    ),
) -> None:
    """Print one session's metadata + an optional head preview."""
    db = _db()
    meta = db.get_session(session_id)
    if meta is None:
        console.print(f"[red]error:[/red] session {session_id!r} not found.")
        raise typer.Exit(1)
    console.print(f"[cyan]id[/cyan]:        {meta.get('id', '')}")
    console.print(f"[cyan]started[/cyan]:   {_format_started(meta.get('started_at'))}")
    if meta.get("ended_at"):
        console.print(
            f"[cyan]ended[/cyan]:     {_format_started(meta.get('ended_at'))}"
        )
    console.print(f"[cyan]platform[/cyan]:  {meta.get('platform', '') or '-'}")
    console.print(f"[cyan]model[/cyan]:     {meta.get('model', '') or '-'}")
    console.print(f"[cyan]title[/cyan]:     {meta.get('title', '') or '-'}")
    console.print(f"[cyan]messages[/cyan]:  {meta.get('message_count', 0)}")
    if head <= 0:
        return
    msgs = db.get_messages(session_id)[:head]
    if not msgs:
        return
    console.print()
    console.print(f"[bold]first {len(msgs)} message(s):[/bold]")
    for i, m in enumerate(msgs, 1):
        preview = (m.content or "")[:200].replace("\n", " ")
        if len(m.content or "") > 200:
            preview += " […]"
        console.print(f"  [dim]{i}.[/dim] [cyan]{m.role}[/cyan]: {preview}")


@session_app.command("fork")
def session_fork(
    session_id: str = typer.Argument(..., help="Source session id to clone."),
    title: str = typer.Option(
        "",
        "--title",
        "-t",
        help="Title for the new session (defaults to '<source title> (fork)').",
    ),
) -> None:
    """Clone a session's history into a new session id.

    Useful for branching a conversation without polluting the source —
    e.g. "what would the agent have said if I'd asked Y instead?".
    The new session inherits the source's platform / model / message
    history; only the id (and title) differ.
    """
    db = _db()
    src = db.get_session(session_id)
    if src is None:
        console.print(f"[red]error:[/red] source session {session_id!r} not found.")
        raise typer.Exit(1)
    msgs = db.get_messages(session_id)
    new_id = uuid.uuid4().hex
    new_title = title or f"{(src.get('title') or '').strip()} (fork)".strip()
    db.create_session(
        new_id,
        platform=src.get("platform", "") or "cli",
        model=src.get("model", "") or "",
        title=new_title,
    )
    if msgs:
        db.append_messages_batch(new_id, msgs)
    console.print(
        f"[green]forked[/green] {session_id} → [cyan]{new_id}[/cyan] "
        f"({len(msgs)} message(s) copied)"
    )
    console.print(f"[dim]continue with: opencomputer chat --resume {new_id}[/dim]")


@session_app.command("resume")
def session_resume(
    session_id: str = typer.Argument(..., help="Session id to continue."),
) -> None:
    """Print the chat-resume command for a session.

    Doesn't spawn ``chat`` itself — typer-inside-typer is fiddly and
    invoking interactive flows from a subcommand has rough edges. The
    caller copy-pastes the printed line.
    """
    src = _db().get_session(session_id)
    if src is None:
        console.print(f"[red]error:[/red] session {session_id!r} not found.")
        raise typer.Exit(1)
    console.print(
        f"[green]ready to resume[/green] [cyan]{session_id}[/cyan] "
        f"({src.get('message_count', 0)} message(s))"
    )
    console.print(f"  [bold]opencomputer chat --resume {session_id}[/bold]")


__all__ = ["session_app"]
