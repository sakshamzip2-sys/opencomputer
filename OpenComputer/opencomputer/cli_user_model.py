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

import re
import time
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.user_model.context import ContextRanker
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


__all__ = ["user_model_app"]
