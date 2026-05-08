"""`opencomputer goal` CLI — Ralph-loop goal management from outside chat.

Closes the deferral from PR #420 (Wave 5 T2): the in-chat ``/goal`` slash
already worked, but the only way to inspect or change a session's goal
from a regular shell was to drop into a chat. This CLI surface makes the
same set/status/pause/resume/clear operations available standalone, which
is essential for:

* scripting (``while ! oc goal status --json | jq -e '.active|not'; do …``)
* cron-driven automation (``oc goal pause`` before nightly tasks)
* IDE integrations that don't run a full TUI

Subcommand surface mirrors the slash exactly so users only have to learn
one model::

    oc goal set "<text>" [--budget N] [--session ID]
    oc goal status [--session ID] [--json]
    oc goal pause [--session ID]
    oc goal resume [--session ID]
    oc goal clear [--session ID]

Default session selection: when ``--session`` is omitted, the most recent
session in the active profile's ``sessions.db`` is used. This matches
what users mean by "the current goal" outside a TUI — the goal of the
session they last interacted with. Empty DBs (no sessions yet) raise a
clear error rather than silently no-op.

Storage is the per-profile ``sessions.db`` resolved via
:func:`opencomputer.agent.config._home`, identical to ``cli_session``,
``cli_audit``, etc. — so a goal set via the CLI is the same row read by
the slash handler in the next interactive session.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer
from rich.console import Console

from opencomputer.agent.config import _home
from opencomputer.agent.state import SessionDB

goal_app = typer.Typer(
    name="goal",
    help="Inspect and manage Ralph-loop goals (set/status/pause/resume/clear).",
)
console = Console()

# Budget default is now resolved at call time from
# :class:`opencomputer.agent.config.GoalsConfig` (Kanban-Goals v2). Pass
# ``None`` through to ``SessionDB.set_session_goal`` and let it consult
# the live config — keeps CLI and slash + agent loop in lockstep.


def _db() -> SessionDB:
    """Open the active profile's sessions DB. Mirrors cli_session._db."""
    db_path: Path = _home() / "sessions.db"
    return SessionDB(db_path)


def _resolve_session_id(db: SessionDB, explicit: str | None) -> str:
    """Return ``explicit`` if given, else the most-recent session id.

    Raises typer.Exit(1) with a clear message when no sessions exist —
    the alternative (silently fail) makes scripting brittle.
    """
    if explicit:
        return explicit
    sessions = db.list_sessions(limit=1)
    if not sessions:
        console.print(
            "[red]error:[/red] no sessions exist yet in the active profile. "
            "Start a chat first, or pass [cyan]--session ID[/cyan]."
        )
        raise typer.Exit(code=1)
    return sessions[0]["id"]


@goal_app.command("set")
def set_cmd(
    text: str = typer.Argument(..., help="Goal text. Quote multi-word goals."),
    budget: int | None = typer.Option(
        None,
        "--budget", "-b",
        help=(
            "Maximum continuation turns before auto-stopping. "
            "Default: goals.max_turns from config (typically 20)."
        ),
        min=1,
    ),
    session: str | None = typer.Option(
        None, "--session", "-s",
        help="Target session ID. Default: most recent session.",
    ),
) -> None:
    """Set or replace the goal on a session.

    Resets ``turns_used`` to 0. If a goal already exists, it is overwritten.
    """
    if not text.strip():
        console.print("[red]error:[/red] goal text cannot be empty.")
        raise typer.Exit(code=1)

    db = _db()
    sid = _resolve_session_id(db, session)
    db.set_session_goal(sid, text=text.strip(), budget=budget)
    g = db.get_session_goal(sid)
    actual_budget = g.budget if g is not None else (budget or 20)
    preview = text if len(text) <= 80 else text[:77] + "..."
    console.print(
        f"[green]⊙ Goal set[/green] ({actual_budget}-turn budget) on session "
        f"[cyan]{sid}[/cyan]:\n  {preview}\n"
        f"  [dim]check progress with [cyan]oc goal status[/cyan][/dim]"
    )


@goal_app.command("status")
def status_cmd(
    session: str | None = typer.Option(
        None, "--session", "-s",
        help="Target session ID. Default: most recent session.",
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="Emit JSON instead of human-readable text. Always exits 0; "
        "absent goals serialize as {\"goal\": null}.",
    ),
) -> None:
    """Show the current goal — text, active flag, turns used vs budget."""
    db = _db()
    sid = _resolve_session_id(db, session)
    g = db.get_session_goal(sid)

    if json_out:
        if g is None:
            payload = {"session_id": sid, "goal": None}
        else:
            payload = {
                "session_id": sid,
                "goal": {
                    "text": g.text,
                    "active": g.active,
                    "turns_used": g.turns_used,
                    "budget": g.budget,
                    "last_judge_reason": g.last_judge_reason,
                },
            }
        console.print(_json.dumps(payload, indent=2))
        return

    if g is None:
        console.print(
            f"[dim]no goal set on session[/dim] [cyan]{sid}[/cyan]\n"
            f"  set one with [cyan]oc goal set \"<text>\"[/cyan]"
        )
        return

    if g.budget_exhausted():
        console.print(
            f"[yellow]⏸ goal paused — {g.turns_used}/{g.budget} turns "
            f"used.[/yellow]\n"
            f"  [bold]goal:[/bold] {g.text}\n"
            f"  Use [cyan]oc goal resume[/cyan] to keep going, or "
            f"[cyan]oc goal clear[/cyan] to stop."
        )
        if g.last_judge_reason:
            console.print(f"  last judge: [dim]{g.last_judge_reason}[/dim]")
        return

    state = "[green]active[/green]" if g.active else "[yellow]paused[/yellow]"
    body = (
        f"[bold]goal[/bold] (session [cyan]{sid}[/cyan]):\n"
        f"  text:   {g.text}\n"
        f"  status: {state}\n"
        f"  turns:  {g.turns_used}/{g.budget}"
    )
    if g.last_judge_reason:
        body += f"\n  last judge: [dim]{g.last_judge_reason}[/dim]"
    console.print(body)


@goal_app.command("pause")
def pause_cmd(
    session: str | None = typer.Option(
        None, "--session", "-s",
        help="Target session ID. Default: most recent session.",
    ),
) -> None:
    """Stop the continuation loop without dropping the goal text.

    Use ``oc goal resume`` to restart with a fresh turn counter.
    """
    db = _db()
    sid = _resolve_session_id(db, session)
    if db.get_session_goal(sid) is None:
        console.print(
            f"[red]no goal set[/red] on session [cyan]{sid}[/cyan] — "
            "nothing to pause."
        )
        raise typer.Exit(code=1)
    db.update_session_goal(sid, active=False)
    console.print(
        f"[yellow]⏸ goal paused[/yellow] on session [cyan]{sid}[/cyan]"
    )


@goal_app.command("resume")
def resume_cmd(
    session: str | None = typer.Option(
        None, "--session", "-s",
        help="Target session ID. Default: most recent session.",
    ),
) -> None:
    """Resume a paused goal and reset the turn counter to 0."""
    db = _db()
    sid = _resolve_session_id(db, session)
    if db.get_session_goal(sid) is None:
        console.print(
            f"[red]no goal set[/red] on session [cyan]{sid}[/cyan] — "
            "nothing to resume."
        )
        raise typer.Exit(code=1)
    db.update_session_goal(
        sid, active=True, turns_used=0, clear_last_judge_reason=True,
    )
    console.print(
        f"[green]↻ goal resumed[/green] on session [cyan]{sid}[/cyan] "
        "(turn counter reset to 0)"
    )


@goal_app.command("clear")
def clear_cmd(
    session: str | None = typer.Option(
        None, "--session", "-s",
        help="Target session ID. Default: most recent session.",
    ),
) -> None:
    """Drop the goal entirely (text → NULL, active → 0).

    The ``goal_budget`` column is preserved so a subsequent
    ``oc goal set`` without ``--budget`` falls back to the existing value.
    """
    db = _db()
    sid = _resolve_session_id(db, session)
    if db.get_session_goal(sid) is None:
        console.print(
            f"[dim]no goal to clear[/dim] on session [cyan]{sid}[/cyan]"
        )
        return
    db.clear_session_goal(sid)
    console.print(
        f"[green]✗ goal cleared[/green] on session [cyan]{sid}[/cyan]"
    )


__all__ = ["goal_app"]
