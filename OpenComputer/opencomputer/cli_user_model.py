"""
``opencomputer user-model`` Typer subapp (Phase 3.C).

Subcommands::

    opencomputer user-model nodes list [--kind X] [--limit 20]
    opencomputer user-model nodes add --kind <K> --value <V>
    opencomputer user-model edges list [--kind X] [--limit 20]
    opencomputer user-model search <query>
    opencomputer user-model import-motifs [--since 7d] [--limit 100]
    opencomputer user-model context [--text ...] [--kinds attribute,goal]
                                    [--top-k 10] [--token-budget 500]

Visibility + manual seeding + ranked retrieval. The ``import-motifs``
command is the main path for converting 3.B motifs into graph state.
"""

from __future__ import annotations

import json
import re
import time
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.user_model.context import ContextRanker
from opencomputer.user_model.decay import DecayEngine
from opencomputer.user_model.drift import DriftDetector
from opencomputer.user_model.drift_store import DriftStore
from opencomputer.user_model.importer import MotifImporter
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.user_model import NodeKind, UserModelQuery

user_model_app = typer.Typer(
    name="user-model",
    help="User-model graph — inspect, seed, rank context (Phase 3.C).",
    no_args_is_help=True,
)

nodes_app = typer.Typer(
    name="nodes",
    help="List / add nodes.",
    no_args_is_help=True,
)
user_model_app.add_typer(nodes_app, name="nodes")

edges_app = typer.Typer(
    name="edges",
    help="List edges.",
    no_args_is_help=True,
)
user_model_app.add_typer(edges_app, name="edges")

# Phase 3.D — decay + drift subapps.
decay_app = typer.Typer(
    name="decay",
    help="Temporal decay — age out edges by per-kind half-life (Phase 3.D).",
    no_args_is_help=True,
)
user_model_app.add_typer(decay_app, name="decay")

drift_app = typer.Typer(
    name="drift",
    help="Drift detection — KL divergence on motif distributions (Phase 3.D).",
    no_args_is_help=True,
)
user_model_app.add_typer(drift_app, name="drift")


_VALID_NODE_KINDS = ("identity", "attribute", "relationship", "goal", "preference")
_VALID_EDGE_KINDS = ("asserts", "contradicts", "supersedes", "derives_from")

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

    Mirrors :func:`opencomputer.cli_inference._parse_duration` so the
    two subapps share the same UX.
    """
    text = text.strip()
    if not text:
        raise typer.BadParameter("duration must be non-empty")
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


def _parse_kinds(value: str | None) -> tuple[NodeKind, ...] | None:
    """Turn a comma-separated ``--kinds`` argument into a tuple. Validates."""
    if value is None:
        return None
    raw = [k.strip() for k in value.split(",") if k.strip()]
    for k in raw:
        if k not in _VALID_NODE_KINDS:
            raise typer.BadParameter(
                f"unknown kind {k!r} — valid: {', '.join(_VALID_NODE_KINDS)}"
            )
    return tuple(raw)  # type: ignore[return-value]


# ─── nodes ────────────────────────────────────────────────────────────


@nodes_app.command("list")
def nodes_list(
    kind: Annotated[
        str | None,
        typer.Option("--kind", help=f"Restrict to one kind: {', '.join(_VALID_NODE_KINDS)}."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Cap on rows returned."),
    ] = 20,
) -> None:
    """Print a Rich table of nodes, newest-last-seen first."""
    if kind is not None and kind not in _VALID_NODE_KINDS:
        raise typer.BadParameter(
            f"--kind must be one of: {', '.join(_VALID_NODE_KINDS)}."
        )
    store = UserModelStore()
    kinds_arg = [kind] if kind else None  # type: ignore[list-item]
    rows = store.list_nodes(kinds=kinds_arg, limit=limit)  # type: ignore[arg-type]
    console = Console()
    if not rows:
        console.print("[dim]no nodes found[/dim]")
        return
    table = Table(title=f"user-model nodes (n={len(rows)})")
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("last_seen", style="dim", no_wrap=True)
    table.add_column("value")
    for n in rows:
        last_seen = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(n.last_seen_at),
        )
        table.add_row(n.kind, f"{n.confidence:.2f}", last_seen, n.value)
    console.print(table)


@nodes_app.command("add")
def nodes_add(
    kind: Annotated[
        str,
        typer.Option("--kind", help=f"One of: {', '.join(_VALID_NODE_KINDS)}."),
    ],
    value: Annotated[
        str,
        typer.Option("--value", help="Human-readable node value."),
    ],
) -> None:
    """Manually insert a node. Useful for debug + seeding."""
    if kind not in _VALID_NODE_KINDS:
        raise typer.BadParameter(
            f"--kind must be one of: {', '.join(_VALID_NODE_KINDS)}."
        )
    store = UserModelStore()
    node = store.upsert_node(kind=kind, value=value)  # type: ignore[arg-type]
    console = Console()
    console.print(
        f"[green]✓[/green] node [cyan]{node.kind}[/cyan] "
        f"[bold]{node.value}[/bold] [dim]({node.node_id[:8]}…)[/dim]"
    )


# ─── edges ────────────────────────────────────────────────────────────


@edges_app.command("list")
def edges_list(
    kind: Annotated[
        str | None,
        typer.Option("--kind", help=f"Restrict to one kind: {', '.join(_VALID_EDGE_KINDS)}."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Cap on rows returned."),
    ] = 20,
) -> None:
    """Print a Rich table of edges, newest first."""
    if kind is not None and kind not in _VALID_EDGE_KINDS:
        raise typer.BadParameter(
            f"--kind must be one of: {', '.join(_VALID_EDGE_KINDS)}."
        )
    store = UserModelStore()
    rows = store.list_edges(kind=kind, limit=limit)  # type: ignore[arg-type]
    console = Console()
    if not rows:
        console.print("[dim]no edges found[/dim]")
        return
    table = Table(title=f"user-model edges (n={len(rows)})")
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("from", style="dim")
    table.add_column("to", style="dim")
    table.add_column("sal", justify="right")
    table.add_column("conf", justify="right")
    table.add_column("rec", justify="right")
    table.add_column("src", justify="right")
    for e in rows:
        table.add_row(
            e.kind,
            f"{e.from_node[:8]}…",
            f"{e.to_node[:8]}…",
            f"{e.salience:.2f}",
            f"{e.confidence:.2f}",
            f"{e.recency_weight:.2f}",
            f"{e.source_reliability:.2f}",
        )
    console.print(table)


# ─── search ───────────────────────────────────────────────────────────


@user_model_app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="FTS5 query against node values.")],
    limit: Annotated[
        int,
        typer.Option("--limit", help="Cap on rows returned."),
    ] = 20,
) -> None:
    """Full-text search across ``node.value``. Uses FTS5 ranking."""
    store = UserModelStore()
    rows = store.search_nodes_fts(query, limit=limit)
    console = Console()
    if not rows:
        console.print("[dim]no matches[/dim]")
        return
    table = Table(title=f"nodes matching {query!r} (n={len(rows)})")
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("value")
    for n in rows:
        table.add_row(n.kind, f"{n.confidence:.2f}", n.value)
    console.print(table)


# ─── import-motifs ────────────────────────────────────────────────────


@user_model_app.command("import-motifs")
def import_motifs(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only motifs newer than this (e.g. '7d', '24h')."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Cap on motifs pulled from MotifStore."),
    ] = 100,
) -> None:
    """Pull recent motifs and materialise them as nodes + edges."""
    since_ts: float | None = None
    if since is not None:
        since_ts = time.time() - _parse_duration(since)
    importer = MotifImporter()
    nodes_added, edges_added = importer.import_recent(since=since_ts, limit=limit)
    console = Console()
    console.print(
        f"[green]imported[/green] "
        f"[bold]{nodes_added}[/bold] new node(s), "
        f"[bold]{edges_added}[/bold] edge(s)"
    )


# ─── context (ranker) ─────────────────────────────────────────────────


@user_model_app.command("context")
def context(
    text: Annotated[
        str | None,
        typer.Option("--text", help="FTS5 query — if set, candidates come from search."),
    ] = None,
    kinds: Annotated[
        str | None,
        typer.Option(
            "--kinds",
            help=f"Comma-separated filter: {', '.join(_VALID_NODE_KINDS)}.",
        ),
    ] = None,
    top_k: Annotated[
        int,
        typer.Option("--top-k", help="Max nodes in the output."),
    ] = 10,
    token_budget: Annotated[
        int | None,
        typer.Option("--token-budget", help="Character-approx token cap."),
    ] = None,
) -> None:
    """Run :class:`ContextRanker` and print the ranked selection."""
    parsed_kinds = _parse_kinds(kinds)
    query = UserModelQuery(
        kinds=parsed_kinds,
        text=text,
        top_k=top_k,
        token_budget=token_budget,
    )
    ranker = ContextRanker()
    snap = ranker.rank(query)
    console = Console()
    if not snap.nodes:
        console.print("[dim]no nodes selected[/dim]")
        return
    table = Table(
        title=(
            f"context snapshot  "
            f"(nodes={len(snap.nodes)}, total_score={snap.total_score:.2f}, "
            f"truncated={snap.truncated})"
        )
    )
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("value")
    for n in snap.nodes:
        table.add_row(n.kind, f"{n.confidence:.2f}", n.value)
    console.print(table)


# ─── decay ────────────────────────────────────────────────────────────


@decay_app.command("run")
def decay_run(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Persist new recency weights; without this, print what would change.",
        ),
    ] = False,
) -> None:
    """Run the temporal-decay pass over every edge.

    Without ``--apply`` prints per-edge "would update" lines without
    touching the store — useful for inspecting the effect of a config
    change before committing. With ``--apply`` walks the full edge set
    and persists new ``recency_weight`` values via
    :meth:`UserModelStore.update_edge_recency_weight`.
    """
    store = UserModelStore()
    engine = DecayEngine(store=store)
    console = Console()
    if apply:
        count = engine.apply_decay()
        console.print(
            f"[green]✓[/green] decay applied — [bold]{count}[/bold] edge(s) updated"
        )
        return
    # Dry-run: list the would-be updates without persisting.
    edges = store.list_edges(limit=1000)
    if not edges:
        console.print("[dim]no edges in store[/dim]")
        return
    table = Table(title=f"decay preview (n={len(edges)}) — use --apply to persist")
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("edge", style="dim")
    table.add_column("age_d", justify="right")
    table.add_column("old_rec", justify="right")
    table.add_column("new_rec", justify="right")
    import time as _time
    now = _time.time()
    for e in edges:
        age_days = max(0.0, (now - e.created_at) / 86400.0)
        new_w = engine.compute_recency_weight(e, now=now)
        table.add_row(
            e.kind,
            f"{e.edge_id[:8]}…",
            f"{age_days:.1f}",
            f"{e.recency_weight:.3f}",
            f"{new_w:.3f}",
        )
    console.print(table)


# ─── drift ────────────────────────────────────────────────────────────


def _format_report_table(report, title: str) -> Table:
    """Build a Rich table summarising a :class:`DriftReport`."""
    table = Table(title=title)
    table.add_column("field", style="cyan", no_wrap=True)
    table.add_column("value")
    table.add_row("report_id", report.report_id)
    table.add_row("created_at", f"{report.created_at:.0f}")
    table.add_row("window_seconds", f"{report.window_seconds:.0f}")
    table.add_row("total_kl", f"{report.total_kl_divergence:.4f}")
    table.add_row("significant", str(report.significant))
    if report.per_kind_drift:
        parts = ", ".join(
            f"{k}={v:.3f}" for k, v in sorted(report.per_kind_drift.items())
        )
        table.add_row("per_kind", parts)
    if report.top_changes:
        lines = [
            (
                f"{c.get('label')} "
                f"(recent={c.get('recent_count')} lifetime={c.get('lifetime_count')} "
                f"ratio={c.get('delta_ratio'):.2f})"
            )
            for c in report.top_changes
        ]
        table.add_row("top_changes", "\n".join(lines))
    return table


@drift_app.command("detect")
def drift_detect(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Persist the resulting report to the drift store.",
        ),
    ] = False,
) -> None:
    """Run a drift-detection pass and print the report.

    Without ``--apply`` the detector runs in read-only mode and the
    report is printed but not persisted. With ``--apply`` the report
    is stashed in :class:`DriftStore` alongside its ``report_id``.
    """
    drift_store = DriftStore() if apply else None
    detector = DriftDetector(drift_store=drift_store)
    report = detector.detect()
    console = Console()
    title = (
        "drift report (persisted)" if apply else "drift report (dry-run; not persisted)"
    )
    console.print(_format_report_table(report, title))


@drift_app.command("list")
def drift_list(
    significant_only: Annotated[
        bool,
        typer.Option("--significant-only", help="Skip reports with significant=False."),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Cap on rows returned."),
    ] = 20,
) -> None:
    """List recent drift reports from the archive."""
    store = DriftStore()
    rows = store.list(significant_only=significant_only, limit=limit)
    console = Console()
    if not rows:
        console.print("[dim]no drift reports stored[/dim]")
        return
    table = Table(title=f"drift reports (n={len(rows)})")
    table.add_column("report_id", style="dim")
    table.add_column("created_at", no_wrap=True)
    table.add_column("total_kl", justify="right")
    table.add_column("significant")
    for r in rows:
        created = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(r.created_at),
        )
        table.add_row(
            f"{r.report_id[:8]}…",
            created,
            f"{r.total_kl_divergence:.4f}",
            "[red]yes[/red]" if r.significant else "no",
        )
    console.print(table)


@drift_app.command("show")
def drift_show(
    report_id: Annotated[
        str,
        typer.Argument(help="UUID of the report to render (prefix-match is NOT supported)."),
    ],
) -> None:
    """Dump a stored drift report as JSON."""
    store = DriftStore()
    report = store.get(report_id)
    console = Console()
    if report is None:
        console.print(f"[bold red]error:[/bold red] no report with id {report_id!r}")
        raise typer.Exit(1)
    payload = {
        "report_id": report.report_id,
        "created_at": report.created_at,
        "window_seconds": report.window_seconds,
        "total_kl_divergence": report.total_kl_divergence,
        "per_kind_drift": dict(report.per_kind_drift),
        "recent_distribution": dict(report.recent_distribution),
        "lifetime_distribution": dict(report.lifetime_distribution),
        "top_changes": [dict(c) for c in report.top_changes],
        "significant": report.significant,
    }
    console.print_json(json.dumps(payload))


__all__ = ["user_model_app"]
