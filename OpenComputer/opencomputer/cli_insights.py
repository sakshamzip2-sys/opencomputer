"""``opencomputer insights`` — per-tool / per-model usage reports.

Reads the v5 ``tool_usage`` table populated by the agent loop on every
tool dispatch. Answers questions like:

- "Which tools have I called the most this month?"
- "Which tool errors out most often?"
- "Is web_search costing me 60% of my time?"

Cost attribution requires per-call provider/cost data which the loop
doesn't yet record (one pricing model per provider lives in
``opencomputer/cost_guard/`` but it's per-day-aggregate). This CLI
ships the *time-and-count* slice now; cost columns join on a follow-up
once the loop also records per-call ``cost_usd`` (deferred so this PR
stays small).
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import default_config
from opencomputer.agent.state import SessionDB

insights_app = typer.Typer(
    name="insights",
    help="Per-tool / per-model usage reports for the active profile.",
    no_args_is_help=False,
    invoke_without_command=True,
)
_console = Console()


@insights_app.callback(invoke_without_command=True)
def insights(
    days: Annotated[
        int,
        typer.Option(
            "--days", "-d",
            help="Time window. Default 30 days. Use 0 for all-time.",
        ),
    ] = 30,
    by: Annotated[
        str,
        typer.Option(
            "--by", "-b",
            help="Group by 'tool' (default), 'model', or 'session'.",
        ),
    ] = "tool",
    limit: Annotated[
        int,
        typer.Option(
            "--limit", "-l",
            help="Max rows to display.",
        ),
    ] = 25,
) -> None:
    """Show per-tool / per-model usage statistics.

    Reads the ``tool_usage`` table (populated by the agent loop on every
    tool call). Empty output means either zero tool calls in the window
    OR the schema migration hasn't been applied yet (it runs on first
    open of any session DB after this build, so a single
    ``opencomputer chat`` run is enough to bootstrap).
    """
    group = "session_id" if by == "session" else by

    db_path = default_config().home / "sessions.db"
    if not db_path.exists():
        _console.print(
            "[dim]No sessions.db yet. Run "
            "[bold]opencomputer chat[/bold] once to create it.[/dim]"
        )
        raise typer.Exit(0)

    db = SessionDB(db_path)
    rows = db.query_tool_usage(
        days=None if days <= 0 else days,
        group_by=group,
    )

    if not rows:
        window = "all-time" if days <= 0 else f"last {days} day{'s' if days != 1 else ''}"
        _console.print(
            f"[dim]No tool_usage rows in the {window} window. "
            "Run a few tool-calling sessions, then try again.[/dim]"
        )
        raise typer.Exit(0)

    title_window = "all-time" if days <= 0 else f"last {days}d"
    table = Table(
        title=f"Tool usage by {by} ({title_window}, top {min(limit, len(rows))} rows)",
        show_lines=False,
    )
    table.add_column(by.capitalize(), style="cyan")
    table.add_column("Calls", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Error %", justify="right")
    table.add_column("Avg ms", justify="right")
    table.add_column("Total s", justify="right")

    for r in rows[:limit]:
        key = r.get("key") or "(none)"
        calls = int(r.get("calls") or 0)
        errors = int(r.get("errors") or 0)
        avg_ms = r.get("avg_duration_ms")
        total_ms = r.get("total_duration_ms")
        err_pct = r.get("error_rate", 0.0) * 100.0
        table.add_row(
            str(key),
            str(calls),
            str(errors),
            f"{err_pct:.1f}%",
            f"{avg_ms:.1f}" if isinstance(avg_ms, (int, float)) else "—",
            f"{(total_ms / 1000.0):.2f}" if isinstance(total_ms, (int, float)) else "—",
        )

    _console.print(table)


__all__ = ["insights_app"]
