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
    ctx: typer.Context,
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
    # When a subcommand was invoked (e.g. ``oc insights llm``), the
    # subcommand handler runs separately — skip the default tool-usage
    # report so the two outputs don't stack.
    if ctx.invoked_subcommand is not None:
        return

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


@insights_app.command("llm")
def insights_llm_command(
    hours: Annotated[
        int,
        typer.Option(
            "--hours", "-H",
            help="Time window in hours. Default 24.",
        ),
    ] = 24,
):
    """Show LLM call activity, cost, and cache-hit ratio over the last N hours.

    Reads ``~/.opencomputer/<profile>/llm_events.jsonl`` written by the
    LLMCallEvent sink (Phase 4 of the quality-foundation work). Until
    Tasks 4.3/4.4 wire the sink into provider extensions, this surface
    will show "No LLM events" — but it works against synthetic events
    in the same shape, so testing is independent of provider wiring.
    """
    import json
    import os
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    home_str = os.environ.get("OPENCOMPUTER_PROFILE_HOME") or str(
        Path.home() / ".opencomputer" / os.environ.get("OPENCOMPUTER_PROFILE", "default")
    )
    log = Path(home_str) / "llm_events.jsonl"
    if not log.exists():
        typer.echo("No LLM events recorded yet.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    by_provider: dict[str, list] = defaultdict(list)
    by_site: dict[str, list] = defaultdict(list)

    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        ts = datetime.fromisoformat(d["ts"])
        if ts < cutoff:
            continue
        by_provider[d["provider"]].append(d)
        if d.get("site"):
            by_site[d["site"]].append(d)

    if not any(by_provider.values()):
        typer.echo(f"No LLM events in last {hours}h.")
        return

    total_calls = sum(len(v) for v in by_provider.values())
    total_cost = sum(
        d.get("cost_usd") or 0 for v in by_provider.values() for d in v
    )
    avg_latency = sum(
        d["latency_ms"] for v in by_provider.values() for d in v
    ) / total_calls

    typer.echo(f"Last {hours}h LLM activity:")
    typer.echo(
        f"  Calls: {total_calls}    Cost: ${total_cost:.2f}    "
        f"Avg latency: {avg_latency:.0f}ms\n"
    )
    typer.echo(
        f"  {'Provider':16} {'Calls':>8} {'Tokens-in':>12} {'Tokens-out':>12} "
        f"{'Cache-hit':>10} {'Cost':>8}"
    )

    for provider, events in by_provider.items():
        calls = len(events)
        toks_in = sum(d["input_tokens"] for d in events)
        toks_out = sum(d["output_tokens"] for d in events)
        cache_create = sum(d["cache_creation_tokens"] for d in events)
        cache_read = sum(d["cache_read_tokens"] for d in events)
        cache_total = cache_create + cache_read
        cache_hit = (cache_read / cache_total * 100) if cache_total else None
        cost = sum(d.get("cost_usd") or 0 for d in events)
        cache_hit_str = f"{cache_hit:.0f}%" if cache_hit is not None else "—"
        typer.echo(
            f"  {provider:16} {calls:>8} {toks_in:>12,} {toks_out:>12,} "
            f"{cache_hit_str:>10} ${cost:>7.2f}"
        )

    typer.echo("\n  Top sites by call count:")
    sorted_sites = sorted(by_site.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]
    for site, events in sorted_sites:
        cost = sum(d.get("cost_usd") or 0 for d in events)
        typer.echo(f"    {site:24} {len(events):>5}  ${cost:>5.2f}")


__all__ = ["insights_app"]
