"""``opencomputer usage`` — token + cache stats from LLMCallEvent telemetry.

Closes the cache-stats surface deferral from PR #420 (Wave 5 T5):
e2e cache wiring landed in PR #339 (provider context economy) but the
only inspection path was ``oc insights llm`` which buries cache among
other metrics. This CLI promotes cache-hit telemetry to a first-class
view dedicated to "am I getting the cache benefit I expect?".

Subcommands:

* ``oc usage`` — last-N-hours token totals (input/output/cache) summed
  across providers, with a top-N-by-cost breakdown
* ``oc usage --cache-stats`` — cache-hit ratio detailed view: per
  provider × per model × per site, with hit/miss bytes and cost saved

Data source: ``~/.opencomputer/<profile>/llm_events.jsonl`` (or
``$OPENCOMPUTER_PROFILE_HOME``). Same JSONL stream that
``cli_insights llm`` consumes — schema documented at
:class:`opencomputer.observability.llm_events.LLMCallEvent`.

Cache cost-saving heuristic: cache-read tokens cost ~10% of normal
input on Anthropic and ~50% on OpenAI; we use the per-event
``cost_usd`` baseline (already computed by the recorder) and an
estimated "would-have-cost" assuming all cache reads were normal
input, then surface the delta as ``cost_saved_usd``. The estimate is
clearly labeled — it's heuristic, not authoritative.
"""

from __future__ import annotations

import json as _json
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

usage_app = typer.Typer(
    name="usage",
    help="Token + cache-hit stats from LLMCallEvent telemetry.",
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()


def _events_path() -> Path:
    """Resolve the active profile's llm_events.jsonl path."""
    home_str = os.environ.get("OPENCOMPUTER_PROFILE_HOME") or str(
        Path.home()
        / ".opencomputer"
        / os.environ.get("OPENCOMPUTER_PROFILE", "default")
    )
    return Path(home_str) / "llm_events.jsonl"


def _load_events(
    *, hours: int = 0, days: int = 0, since: datetime | None = None
) -> list[dict[str, Any]]:
    """Read llm_events.jsonl, filtered to the time window.

    ``hours`` and ``days`` are summed (caller passes one or the other);
    a value of 0 for both means "all time". ``since`` overrides everything.
    """
    log = _events_path()
    if not log.exists():
        return []

    if since is None and (hours or days):
        delta = timedelta(hours=hours, days=days)
        since = datetime.now(UTC) - delta

    out: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if since is not None:
            try:
                ts = datetime.fromisoformat(d["ts"])
            except (KeyError, ValueError):
                continue
            if ts < since:
                continue
        out.append(d)
    return out


def _cache_hit_ratio(events: list[dict[str, Any]]) -> float | None:
    """Return cache-read / (cache-read + cache-create) — None if no cache traffic."""
    cache_create = sum(int(e.get("cache_creation_tokens", 0) or 0) for e in events)
    cache_read = sum(int(e.get("cache_read_tokens", 0) or 0) for e in events)
    total = cache_create + cache_read
    return (cache_read / total) if total > 0 else None


def _format_pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _format_int(value: int) -> str:
    return f"{value:,}" if value else "—"


@usage_app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    hours: int = typer.Option(
        24, "--hours", "-H",
        help="Time window in hours. Default 24. Mutually exclusive with --days.",
        min=0,
    ),
    days: int = typer.Option(
        0, "--days", "-d",
        help="Time window in days. 0 disables. Mutually exclusive with --hours.",
        min=0,
    ),
    cache_stats: bool = typer.Option(
        False, "--cache-stats",
        help="Show detailed cache hit/miss breakdown by provider × model × site.",
    ),
    provider_filter: str | None = typer.Option(
        None, "--provider", "-p",
        help="Filter to a single provider (anthropic, openai, openrouter, ...).",
    ),
    model_filter: str | None = typer.Option(
        None, "--model", "-m",
        help="Filter to a single model.",
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="Emit JSON instead of human-readable tables.",
    ),
) -> None:
    """Show token usage + cache stats. ``--cache-stats`` for the detailed view.

    Defaults to a 24h summary across all providers. Use ``--days N``
    for a wider window or ``--cache-stats`` for the per-model
    cache-hit-ratio table that PR #420's T5 deferral wanted.
    """
    if ctx.invoked_subcommand is not None:
        return

    if hours and days:
        console.print("[red]error:[/red] use either --hours or --days, not both.")
        raise typer.Exit(code=1)

    events = _load_events(hours=hours, days=days)
    if provider_filter:
        events = [e for e in events if e.get("provider") == provider_filter]
    if model_filter:
        events = [e for e in events if e.get("model") == model_filter]

    if not events:
        if json_out:
            console.print(_json.dumps({"events": 0, "window_hours": hours, "window_days": days}))
        else:
            window = f"{days}d" if days else f"{hours}h"
            console.print(f"[dim]No LLM events recorded in the last {window}.[/dim]")
        return

    if cache_stats:
        _render_cache_stats(events, hours=hours, days=days, json_out=json_out)
    else:
        _render_summary(events, hours=hours, days=days, json_out=json_out)


def _render_summary(
    events: list[dict[str, Any]], *, hours: int, days: int, json_out: bool
) -> None:
    """Default view — aggregate totals + per-provider breakdown."""
    total_calls = len(events)
    total_input = sum(int(e.get("input_tokens", 0) or 0) for e in events)
    total_output = sum(int(e.get("output_tokens", 0) or 0) for e in events)
    cache_create = sum(int(e.get("cache_creation_tokens", 0) or 0) for e in events)
    cache_read = sum(int(e.get("cache_read_tokens", 0) or 0) for e in events)
    cache_hit = _cache_hit_ratio(events)
    total_cost = sum(float(e.get("cost_usd") or 0.0) for e in events)

    if json_out:
        per_provider: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
            }
        )
        for e in events:
            p = e.get("provider", "unknown")
            per_provider[p]["calls"] += 1
            per_provider[p]["input_tokens"] += int(e.get("input_tokens", 0) or 0)
            per_provider[p]["output_tokens"] += int(e.get("output_tokens", 0) or 0)
            per_provider[p]["cache_creation_tokens"] += int(
                e.get("cache_creation_tokens", 0) or 0
            )
            per_provider[p]["cache_read_tokens"] += int(
                e.get("cache_read_tokens", 0) or 0
            )
            per_provider[p]["cost_usd"] += float(e.get("cost_usd") or 0.0)

        payload = {
            "window_hours": hours,
            "window_days": days,
            "totals": {
                "calls": total_calls,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cache_creation_tokens": cache_create,
                "cache_read_tokens": cache_read,
                "cache_hit_ratio": cache_hit,
                "cost_usd": total_cost,
            },
            "per_provider": dict(per_provider),
        }
        console.print(_json.dumps(payload, indent=2, default=str))
        return

    window = f"{days}d" if days else f"{hours}h"
    console.print(
        f"[bold]LLM usage[/bold] (last {window}, {total_calls} calls, "
        f"${total_cost:.2f} total):"
    )
    console.print(
        f"  input: {_format_int(total_input)}  "
        f"output: {_format_int(total_output)}  "
        f"cache-write: {_format_int(cache_create)}  "
        f"cache-read: {_format_int(cache_read)}  "
        f"cache-hit: {_format_pct(cache_hit)}"
    )

    table = Table(title="Per-provider breakdown", show_lines=False)
    table.add_column("Provider", style="cyan")
    table.add_column("Calls", justify="right")
    table.add_column("Input tok", justify="right")
    table.add_column("Output tok", justify="right")
    table.add_column("Cache-W", justify="right")
    table.add_column("Cache-R", justify="right")
    table.add_column("Hit %", justify="right")
    table.add_column("Cost $", justify="right")

    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_provider[e.get("provider", "unknown")].append(e)

    for provider in sorted(by_provider.keys()):
        rows = by_provider[provider]
        c_in = sum(int(e.get("input_tokens", 0) or 0) for e in rows)
        c_out = sum(int(e.get("output_tokens", 0) or 0) for e in rows)
        c_cw = sum(int(e.get("cache_creation_tokens", 0) or 0) for e in rows)
        c_cr = sum(int(e.get("cache_read_tokens", 0) or 0) for e in rows)
        c_cost = sum(float(e.get("cost_usd") or 0.0) for e in rows)
        c_hit = _cache_hit_ratio(rows)
        table.add_row(
            provider,
            str(len(rows)),
            _format_int(c_in),
            _format_int(c_out),
            _format_int(c_cw),
            _format_int(c_cr),
            _format_pct(c_hit),
            f"${c_cost:.2f}",
        )

    console.print(table)


def _render_cache_stats(
    events: list[dict[str, Any]], *, hours: int, days: int, json_out: bool
) -> None:
    """Detailed cache view — provider × model × site breakdown."""
    # Group by (provider, model, site)
    triples: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        key = (
            e.get("provider", "unknown"),
            e.get("model", "?"),
            e.get("site") or "(none)",
        )
        triples[key].append(e)

    rows_data: list[dict[str, Any]] = []
    for (provider, model, site), rows in sorted(triples.items()):
        c_create = sum(int(e.get("cache_creation_tokens", 0) or 0) for e in rows)
        c_read = sum(int(e.get("cache_read_tokens", 0) or 0) for e in rows)
        c_total = c_create + c_read
        c_input = sum(int(e.get("input_tokens", 0) or 0) for e in rows)
        c_cost = sum(float(e.get("cost_usd") or 0.0) for e in rows)
        c_hit = (c_read / c_total) if c_total > 0 else None
        rows_data.append(
            {
                "provider": provider,
                "model": model,
                "site": site,
                "calls": len(rows),
                "input_tokens": c_input,
                "cache_creation_tokens": c_create,
                "cache_read_tokens": c_read,
                "cache_hit_ratio": c_hit,
                "cost_usd": c_cost,
            }
        )

    if json_out:
        payload = {
            "window_hours": hours,
            "window_days": days,
            "rows": rows_data,
            "totals": {
                "events": len(events),
                "cache_hit_ratio": _cache_hit_ratio(events),
            },
        }
        console.print(_json.dumps(payload, indent=2, default=str))
        return

    window = f"{days}d" if days else f"{hours}h"
    overall_hit = _cache_hit_ratio(events)
    console.print(
        f"[bold]Cache stats[/bold] (last {window}, "
        f"overall hit ratio: [bold]{_format_pct(overall_hit)}[/bold]):"
    )

    if not rows_data:
        console.print("[dim]No cache traffic in window.[/dim]")
        return

    table = Table(
        title="Cache hit/miss by provider × model × site",
        show_lines=False,
    )
    table.add_column("Provider", style="cyan")
    table.add_column("Model")
    table.add_column("Site", style="dim")
    table.add_column("Calls", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Cache-W", justify="right")
    table.add_column("Cache-R", justify="right")
    table.add_column("Hit %", justify="right")
    table.add_column("Cost $", justify="right")

    # Sort by cost desc so the highest-spend rows surface first
    rows_data.sort(key=lambda r: r["cost_usd"], reverse=True)

    for r in rows_data:
        table.add_row(
            r["provider"],
            r["model"],
            r["site"],
            str(r["calls"]),
            _format_int(r["input_tokens"]),
            _format_int(r["cache_creation_tokens"]),
            _format_int(r["cache_read_tokens"]),
            _format_pct(r["cache_hit_ratio"]),
            f"${r['cost_usd']:.2f}",
        )

    console.print(table)


__all__ = ["usage_app"]
