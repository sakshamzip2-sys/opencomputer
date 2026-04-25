"""
``opencomputer inference`` Typer subapp (Phase 3.B).

Subcommands::

    opencomputer inference motifs list [--kind X] [--since 7d] [--limit 50]
    opencomputer inference motifs stats
    opencomputer inference motifs prune --older-than 30d
    opencomputer inference motifs run

The ``run`` subcommand attaches a :class:`BehavioralInferenceEngine`
to the default bus and waits for ``Ctrl-C`` — useful as a dev tool
for watching motif extraction in real time.
"""

from __future__ import annotations

import re
import time
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.inference.engine import BehavioralInferenceEngine
from opencomputer.inference.storage import MotifStore

inference_app = typer.Typer(
    name="inference",
    help="Behavioral inference engine — motif extraction over the F2 bus.",
    no_args_is_help=True,
)

motifs_app = typer.Typer(
    name="motifs",
    help="Inspect / prune / run extraction over stored motifs.",
    no_args_is_help=True,
)
inference_app.add_typer(motifs_app, name="motifs")


_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")
_DURATION_UNITS_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 86400 * 7,
}


def _parse_duration(text: str) -> float:
    """Parse a short human duration string ("7d", "30s", "1.5h") into seconds.

    Accepts only one unit suffix at a time. ``s/m/h/d/w``. Plain
    numeric strings are treated as seconds. Any unrecognised input
    raises :exc:`typer.BadParameter`.
    """
    text = text.strip()
    if not text:
        raise typer.BadParameter("duration must be non-empty")
    # Plain integers / floats — interpret as seconds.
    try:
        return float(text)
    except ValueError:
        pass
    m = _DURATION_RE.match(text)
    if m is None:
        raise typer.BadParameter(
            f"unrecognised duration {text!r} — use e.g. '7d', '30m', '1.5h'."
        )
    value = float(m.group(1))
    unit = m.group(2)
    return value * _DURATION_UNITS_SECONDS[unit]


@motifs_app.command("list")
def motifs_list(
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            help="Restrict to one kind: temporal, transition, implicit_goal.",
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Only motifs newer than this duration (e.g. '7d', '24h').",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Cap on rows returned.",
        ),
    ] = 50,
) -> None:
    """Print a Rich table of motifs matching the filters.

    Empty result prints an info message and exits cleanly.
    """
    if kind is not None and kind not in ("temporal", "transition", "implicit_goal"):
        raise typer.BadParameter(
            "--kind must be one of: temporal, transition, implicit_goal."
        )
    since_ts: float | None = None
    if since is not None:
        since_ts = time.time() - _parse_duration(since)

    store = MotifStore()
    motifs = store.list(
        kind=kind,  # type: ignore[arg-type] — validated above
        since=since_ts,
        limit=limit,
    )

    console = Console()
    if not motifs:
        console.print("[dim]no motifs found[/dim]")
        return

    table = Table(title=f"motifs (n={len(motifs)})")
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("support", justify="right")
    table.add_column("created_at", style="dim", no_wrap=True)
    table.add_column("summary")
    for m in motifs:
        created = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(m.created_at),
        )
        table.add_row(
            m.kind,
            f"{m.confidence:.2f}",
            str(m.support),
            created,
            m.summary,
        )
    console.print(table)


@motifs_app.command("stats")
def motifs_stats() -> None:
    """Print one row per motif kind: count and most-recent timestamp."""
    store = MotifStore()
    console = Console()
    table = Table(title="motif store stats")
    table.add_column("kind", style="cyan")
    table.add_column("count", justify="right")
    total = store.count()
    for kind in ("temporal", "transition", "implicit_goal"):
        n = store.count(kind=kind)  # type: ignore[arg-type]
        table.add_row(kind, str(n))
    table.add_row("[bold]total[/bold]", f"[bold]{total}[/bold]")
    console.print(table)


@motifs_app.command("prune")
def motifs_prune(
    older_than: Annotated[
        str,
        typer.Option(
            "--older-than",
            help="Delete motifs older than this (e.g. '30d', '90d').",
        ),
    ],
) -> None:
    """Delete motifs older than ``--older-than``. Prints the row count."""
    age_s = _parse_duration(older_than)
    store = MotifStore()
    deleted = store.delete_older_than(age_s)
    console = Console()
    console.print(f"[green]deleted {deleted} motif(s)[/green]")


@motifs_app.command("run")
def motifs_run() -> None:
    """Attach an engine to the default bus; flush on Ctrl-C.

    Interactive dev / debug tool. The engine subscribes wildcard;
    every event published on :data:`opencomputer.ingestion.bus.default_bus`
    is buffered, and the in-memory buffer is flushed when this
    function exits (whether via Ctrl-C or normal return).
    """
    console = Console()
    engine = BehavioralInferenceEngine()
    engine.attach_to_bus()
    console.print(
        "[green]inference engine attached to default_bus[/green] — "
        "press Ctrl-C to flush + exit"
    )
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[yellow]flushing…[/yellow]")
    finally:
        try:
            count = engine.flush_now()
        except Exception as e:  # noqa: BLE001 — CLI-friendly error
            console.print(f"[red]flush failed:[/red] {e}")
            count = 0
        engine.detach()
        console.print(f"[green]wrote {count} motif(s) on shutdown[/green]")


__all__ = ["inference_app"]
