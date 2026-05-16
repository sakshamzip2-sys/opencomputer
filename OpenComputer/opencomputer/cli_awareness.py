"""V2.C — ``opencomputer awareness {patterns,personas} {list,mute,unmute}``.

Two subgroups under one top-level group:

* ``awareness patterns`` — life-event pattern controls (T1/T2 registry).
  Mute state is persisted at ``$OPENCOMPUTER_HOME/awareness/muted_patterns.json``
  (atomic truncate-then-write; single-user). On agent start the registry
  reads this file once and applies the muted set.

* ``awareness personas`` — plural-persona controls. Registry is implemented
  in T4; until then ``personas list`` prints a stub line so users running
  ``--help`` against a partially-built tree don't hit ``ImportError``.

Capability claims for these flows live in ``F1_CAPABILITIES`` under
``awareness.life_event.*`` and ``awareness.persona.*`` (all IMPLICIT — see
the taxonomy comment for the rationale).
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from opencomputer.user_model.store import UserModelStore
    from plugin_sdk.user_model import Node

awareness_app = typer.Typer(
    help=(
        "Layered Awareness — review & correct what the agent knows about "
        "you, plus life-event pattern and persona controls."
    )
)
patterns_app = typer.Typer(help="Life-event pattern controls")
personas_app = typer.Typer(help="Plural-persona controls")
awareness_app.add_typer(patterns_app, name="patterns")
awareness_app.add_typer(personas_app, name="personas")


def _muted_state_path() -> Path:
    """Return path to the persisted muted-patterns JSON list.

    Resolved every call (not cached) so tests that monkey-patch
    ``OPENCOMPUTER_HOME`` per-test pick up the right tmp path.
    """
    from opencomputer.agent.config import _home

    return _home() / "awareness" / "muted_patterns.json"


def _load_muted() -> list[str]:
    """Load persisted muted pattern IDs. Tolerates missing/corrupt file."""
    path = _muted_state_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def _save_muted(muted: list[str]) -> None:
    """Persist muted pattern IDs (truncate-then-write)."""
    path = _muted_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(muted))


def _registry_pattern_ids() -> set[str]:
    """Return the set of known pattern IDs from the default registry."""
    from opencomputer.awareness.life_events.registry import LifeEventRegistry

    reg = LifeEventRegistry()
    return {pid for pid, _surf, _muted in reg.list_patterns()}


@patterns_app.command("list")
def patterns_list() -> None:
    """List all registered life-event patterns + their muted state."""
    from opencomputer.awareness.life_events.registry import LifeEventRegistry

    reg = LifeEventRegistry()
    persisted_muted = set(_load_muted())
    typer.echo(f"{'pattern_id':30s} {'surfacing':10s} {'muted':6s}")
    for pattern_id, surfacing, in_memory_muted in reg.list_patterns():
        muted = in_memory_muted or (pattern_id in persisted_muted)
        typer.echo(f"{pattern_id:30s} {surfacing:10s} {'yes' if muted else 'no':6s}")


@patterns_app.command("mute")
def patterns_mute(
    pattern_id: str = typer.Argument(..., help="Pattern ID to mute (see `awareness patterns list`)."),
) -> None:
    """Mute a life-event pattern (silent for the rest of this session AND saved)."""
    valid_ids = _registry_pattern_ids()
    if pattern_id not in valid_ids:
        typer.echo(f"Unknown pattern: {pattern_id}", err=True)
        typer.echo(f"Known patterns: {', '.join(sorted(valid_ids))}", err=True)
        raise typer.Exit(1)

    muted = _load_muted()
    if pattern_id not in muted:
        muted.append(pattern_id)
    _save_muted(muted)
    typer.echo(f"Muted: {pattern_id}")


@patterns_app.command("unmute")
def patterns_unmute(
    pattern_id: str = typer.Argument(..., help="Pattern ID to unmute."),
) -> None:
    """Unmute a previously-muted life-event pattern."""
    state_path = _muted_state_path()
    if not state_path.exists():
        typer.echo("Nothing muted (no state file).")
        return
    muted = _load_muted()
    if pattern_id in muted:
        muted.remove(pattern_id)
    _save_muted(muted)
    typer.echo(f"Unmuted: {pattern_id}")


@personas_app.command("list")
def personas_list() -> None:
    """List all registered personas.

    The persona registry is implemented in V2.C-T4. Until that lands this
    command prints a stub instead of raising ``ImportError`` so the CLI
    surface stays usable.
    """
    try:
        from opencomputer.awareness.personas.registry import (  # type: ignore[import-not-found]
            list_personas,
        )
    except ImportError:
        typer.echo("No personas registered yet (V2.C-T4 pending)")
        return

    personas = list_personas()
    if not personas:
        typer.echo("No personas registered.")
        return
    typer.echo(f"{'persona_id':20s} {'description':50s}")
    for p in personas:
        # Tolerate either {"id": ..., "description": ...} dicts or objects
        # exposing those attributes; pick whichever shape T4 ships.
        if isinstance(p, dict):
            pid = str(p.get("id", ""))
            desc = str(p.get("description", ""))
        else:
            pid = str(getattr(p, "id", ""))
            desc = str(getattr(p, "description", ""))
        typer.echo(f"{pid:20s} {desc:50s}")


# ─────────────────────────────────────────────────────────────────────
# F4 user-model facts — review / explain / forget / correct (M1)
#
# These commands operate on the user-model graph at
# ``<profile_home>/user_model/graph.sqlite``. They are the user-facing
# trust surface: inspect what the agent believes about you, and correct
# it when it is wrong. They extend — not replace — the developer-facing
# ``oc user-model`` subapp in ``cli_user_model.py``.
# ─────────────────────────────────────────────────────────────────────

#: Kind priority for display ranking — mirrors
#: ``PromptBuilder.build_user_facts`` so ``review`` previews the prompt's
#: <user-facts> block faithfully. ``relationship`` is not prompt-injected
#: today; it sorts last so inspection still surfaces it.
_KIND_ORDER: dict[str, int] = {
    "identity": 0,
    "goal": 1,
    "preference": 2,
    "attribute": 3,
    "relationship": 4,
}


def _node_is_deleted(node: Node) -> bool:
    """Return True if ``node`` carries the M1 soft-delete tombstone flag.

    ``forget`` (without ``--hard``) sets ``metadata["deleted"] = True``
    rather than dropping the row, so the eviction stays auditable and
    reversible. ``review`` hides these unless ``--deleted`` is passed.
    """
    try:
        return bool(node.metadata.get("deleted", False))
    except AttributeError:
        return False


def _count_incoming_contradicts(store: UserModelStore, node_id: str) -> int:
    """Count ``contradicts`` edges pointing AT ``node_id``.

    A non-zero count means another fact (written later, or by a more
    reliable source) disputes this one — the signal M4 turns into a
    ranking penalty. Until ``correct`` is used the count is always 0.
    """
    return len(
        store.list_edges(kind="contradicts", to_node=node_id, limit=10_000)
    )


def _node_provenance(store: UserModelStore, node_id: str) -> str:
    """Return a coarse provenance label for a node from its incident edges.

    ``Node`` carries no ``source`` column — only ``Edge`` does — so a
    node's provenance is the dominant ``source`` across its incident
    edges. Orphan nodes (e.g. profile-bootstrap rows upserted without an
    edge) return ``"—"``. A ``+`` suffix marks a node with mixed sources.
    """
    incident = [
        *store.list_edges(from_node=node_id, limit=200),
        *store.list_edges(to_node=node_id, limit=200),
    ]
    sources = [e.source for e in incident if e.source]
    if not sources:
        return "—"
    counts = Counter(sources)
    dominant, _ = counts.most_common(1)[0]
    return dominant if len(counts) == 1 else f"{dominant}+"


def _rank_for_review(nodes: list[Node]) -> list[Node]:
    """Sort nodes by (kind priority, descending confidence).

    Same ordering ``build_user_facts`` applies, so ``review`` lists
    facts in the order the agent would inject them into the prompt.
    """
    return sorted(
        nodes,
        key=lambda n: (_KIND_ORDER.get(n.kind, 99), -n.confidence),
    )


@awareness_app.command("review")
def review(
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Show every fact, not just the top 50."),
    ] = False,
    deleted: Annotated[
        bool,
        typer.Option("--deleted", help="Include soft-deleted (forgotten) facts."),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Cap on facts shown (ignored with --all)."),
    ] = 50,
    needs_review: Annotated[
        bool,
        typer.Option(
            "--needs-review",
            help="Show only facts flagged by `awareness migrate`.",
        ),
    ] = False,
) -> None:
    """Show what the agent currently believes about you.

    Renders the top-K user-model facts in the same priority order the
    prompt uses, with provenance and a contradiction flag. Scan this
    before running ``forget`` / ``correct``.
    """
    from opencomputer.user_model.store import UserModelStore

    store = UserModelStore()
    total_in_store = store.count_nodes()
    fetch = max(500, total_in_store) if show_all else 500
    nodes = store.list_nodes(limit=fetch)
    if not deleted:
        nodes = [n for n in nodes if not _node_is_deleted(n)]
    if needs_review:
        nodes = [n for n in nodes if n.metadata.get("needs_review")]
    ranked = _rank_for_review(nodes)
    total = len(ranked)
    shown = ranked if show_all else ranked[: max(0, limit)]

    console = Console()
    if not shown:
        console.print("[dim]no facts recorded yet[/dim]")
        return

    table = Table(
        title=(
            "awareness — what I know about you "
            f"(showing {len(shown)} of {total} facts)"
        )
    )
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("last seen", style="dim", no_wrap=True)
    table.add_column("source", style="dim", no_wrap=True)
    table.add_column("flags", no_wrap=True)
    table.add_column("value", overflow="fold")

    contradicted = 0
    for n in shown:
        n_contra = _count_incoming_contradicts(store, n.node_id)
        if n_contra:
            contradicted += 1
        flag = f"[red]⚠×{n_contra}[/red]" if n_contra else ""
        deleted_mark = " [dim](forgotten)[/dim]" if _node_is_deleted(n) else ""
        last_seen = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(n.last_seen_at)
        )
        table.add_row(
            n.node_id[:8],
            n.kind,
            f"{n.confidence:.2f}",
            last_seen,
            _node_provenance(store, n.node_id),
            flag,
            n.value + deleted_mark,
        )
    console.print(table)

    if contradicted:
        console.print(
            f"[dim]⚠ — {contradicted} fact(s) contradicted by a newer or "
            "higher-confidence signal; run [bold]oc awareness explain "
            "<id>[/bold] for detail.[/dim]"
        )
    if not show_all and total > len(shown):
        console.print(
            f"[dim]… {total - len(shown)} more — run with [bold]--all[/bold] "
            "to see everything.[/dim]"
        )


def _resolve_node_id(store: UserModelStore, id_or_prefix: str) -> Node:
    """Resolve a full node id OR a unique id prefix to a single Node.

    ``review`` prints 8-char id prefixes; the action commands
    (``explain`` / ``forget`` / ``correct``) accept either the full id
    or any unique prefix, git-style. Prints an error and raises
    ``typer.Exit(1)`` on a missing id or an ambiguous prefix — soft-
    deleted nodes are still resolvable so they can be inspected.
    """
    console = Console()
    exact = store.get_node(id_or_prefix)
    if exact is not None:
        return exact
    candidates = [
        n for n in store.list_nodes(limit=100_000)
        if n.node_id.startswith(id_or_prefix)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        console.print(
            f"[bold red]error:[/bold red] no fact with id {id_or_prefix!r}. "
            "Run [bold]oc awareness review[/bold] to list ids."
        )
        raise typer.Exit(1)
    console.print(
        f"[bold red]error:[/bold red] ambiguous id prefix {id_or_prefix!r} — "
        f"it matches {len(candidates)} facts:"
    )
    for n in candidates[:10]:
        console.print(f"  [dim]{n.node_id}[/dim]  ({n.kind}) {n.value[:50]}")
    if len(candidates) > 10:
        console.print(f"  [dim]… and {len(candidates) - 10} more[/dim]")
    raise typer.Exit(1)


def _explain_session(console: Console, query: str | None) -> None:
    """Render the reranker score breakdown for the prompt's top facts.

    Shows, per fact, the kind / confidence / recency / BM25 sub-scores
    and the composite — the exact maths ``build_user_facts`` uses to
    pick the <user-facts> block. ``query`` simulates a session opening
    message; ``None`` shows the context-free breakdown.
    """
    from opencomputer.user_model.reranker import (
        SessionContext,
        UserFactsReranker,
    )
    from opencomputer.user_model.store import UserModelStore

    store = UserModelStore()
    nodes = [
        n
        for n in store.list_nodes(limit=500)
        if not _node_is_deleted(n) and not n.metadata.get("needs_review")
    ]
    if not nodes:
        console.print("[dim]no facts to rank[/dim]")
        return
    ctx = SessionContext(recent_messages=(query,) if query else ())
    scored = UserFactsReranker().score(nodes, ctx)[:20]
    label = f"query={query!r}" if query else "context-free"
    table = Table(title=f"reranker score breakdown — top {len(scored)} ({label})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("value")
    table.add_column("kind✦", justify="right")
    table.add_column("conf✦", justify="right")
    table.add_column("recency✦", justify="right")
    table.add_column("bm25✦", justify="right")
    table.add_column("score", justify="right", style="bold")
    for i, sf in enumerate(scored, start=1):
        b = sf.breakdown
        table.add_row(
            str(i), sf.node.kind, sf.node.value[:40],
            f"{b['kind']:.2f}", f"{b['confidence']:.2f}",
            f"{b['recency']:.2f}", f"{b['bm25']:.2f}",
            f"{sf.score:.3f}",
        )
    console.print(table)
    console.print(
        "[dim]✦ = per-term sub-score (0–1); score = weighted blend.[/dim]"
    )


@awareness_app.command("explain")
def explain(
    node_id: Annotated[
        str | None,
        typer.Argument(
            help="Node id or unique id prefix (see `awareness review`). "
            "Omit when using --session."
        ),
    ] = None,
    session: Annotated[
        bool,
        typer.Option(
            "--session",
            help="Show the reranker score breakdown for the top facts.",
        ),
    ] = False,
    query: Annotated[
        str | None,
        typer.Option(
            "--query",
            help="With --session: simulate a session opening message.",
        ),
    ] = None,
) -> None:
    """Show provenance for one fact, or the reranker breakdown.

    ``explain <id>`` renders the node's fields, every incident edge, and
    — per edge — the stored vs. live decay-adjusted recency weight.
    ``explain --session`` instead shows, for the prompt's top facts, the
    per-term reranker score breakdown (kind / confidence / recency /
    BM25) and the composite; ``--query`` drives the BM25 term.
    """
    from opencomputer.user_model.decay import DecayEngine
    from opencomputer.user_model.store import UserModelStore

    console = Console()
    if session:
        _explain_session(console, query)
        return
    if not node_id:
        console.print(
            "[bold red]error:[/bold red] give a fact id, or use "
            "[bold]--session[/bold] for the reranker breakdown."
        )
        raise typer.Exit(1)

    store = UserModelStore()
    node = _resolve_node_id(store, node_id)

    status = (
        "[red]forgotten[/red]" if _node_is_deleted(node)
        else "[green]active[/green]"
    )
    detail = Table(title=f"fact {node.node_id}", show_header=False, box=None)
    detail.add_column("field", style="cyan", no_wrap=True)
    detail.add_column("value")
    detail.add_row("kind", node.kind)
    detail.add_row("value", node.value)
    detail.add_row("confidence", f"{node.confidence:.2f}")
    detail.add_row("status", status)
    detail.add_row(
        "created",
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(node.created_at)),
    )
    detail.add_row(
        "last seen",
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(node.last_seen_at)),
    )
    detail.add_row("provenance", _node_provenance(store, node.node_id))
    if node.metadata:
        detail.add_row("metadata", json.dumps(dict(node.metadata)))
    console.print(detail)

    outgoing = store.list_edges(from_node=node.node_id, limit=500)
    incoming = store.list_edges(to_node=node.node_id, limit=500)
    if not outgoing and not incoming:
        console.print("[dim]no incident edges (orphan node)[/dim]")
        return

    engine = DecayEngine(store=store)
    now = time.time()
    edges_table = Table(
        title=f"incident edges ({len(outgoing) + len(incoming)})"
    )
    edges_table.add_column("dir", style="cyan", no_wrap=True)
    edges_table.add_column("kind", no_wrap=True)
    edges_table.add_column("other fact", style="dim")
    edges_table.add_column("conf", justify="right")
    edges_table.add_column("stored rec", justify="right")
    edges_table.add_column("decay→now", justify="right")
    edges_table.add_column("source", style="dim")
    for direction, edges in (("out →", outgoing), ("in ←", incoming)):
        for e in edges:
            other_id = e.to_node if direction.startswith("out") else e.from_node
            other = store.get_node(other_id)
            other_label = other.value[:36] if other else other_id[:8]
            live = engine.compute_recency_weight(e, now=now)
            edges_table.add_row(
                direction, e.kind, other_label,
                f"{e.confidence:.2f}", f"{e.recency_weight:.3f}",
                f"{live:.3f}", e.source,
            )
    console.print(edges_table)
    console.print(
        "[dim]'stored rec' is the persisted recency_weight; 'decay→now' is "
        "what temporal decay would set it to right now — a large gap means "
        "the decay scheduler has not run.[/dim]"
    )


@awareness_app.command("forget")
def forget(
    node_id: Annotated[
        str,
        typer.Argument(help="Node id or unique id prefix (see `awareness review`)."),
    ],
    hard: Annotated[
        bool,
        typer.Option("--hard", help="Drop the row outright instead of soft-deleting."),
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Required to forget an identity fact."),
    ] = False,
) -> None:
    """Forget a fact the agent learned wrong.

    Default is a reversible soft-delete: the row stays, flagged
    ``deleted``, and is hidden from prompts and ``review``. ``--hard``
    drops the row and its incident edges. Identity facts are
    foundational — forgetting one requires ``--confirm``.
    """
    from opencomputer.user_model.store import UserModelStore

    store = UserModelStore()
    node = _resolve_node_id(store, node_id)
    console = Console()

    if node.kind == "identity" and not confirm:
        incident = len(
            store.list_edges(from_node=node.node_id, limit=10_000)
        ) + len(store.list_edges(to_node=node.node_id, limit=10_000))
        console.print(
            f"[bold yellow]refused:[/bold yellow] [cyan]{node.value}[/cyan] is "
            f"an [bold]identity[/bold] fact with {incident} incident edge(s). "
            "Identity facts are foundational — forgetting one can break later "
            "runs.\nRe-run with [bold]--confirm[/bold] if you are sure."
        )
        raise typer.Exit(1)

    if hard:
        removed = store.delete_node(node.node_id)
        if removed:
            console.print(
                f"[green]✓[/green] hard-deleted [cyan]{node.kind}[/cyan] "
                f"[bold]{node.value}[/bold] [dim]({node.node_id[:8]})[/dim] "
                "— row and incident edges removed."
            )
        else:
            console.print(f"[dim]nothing deleted for {node.node_id[:8]}[/dim]")
        return

    if _node_is_deleted(node):
        console.print(
            f"[dim]{node.value} is already forgotten ({node.node_id[:8]})[/dim]"
        )
        return

    # Soft-delete: keep the row, flag it. Reversible and auditable —
    # `review --deleted` still surfaces it, `explain` still resolves it.
    # Uses update_node_metadata (in-place UPDATE) NOT insert_node, so the
    # node's incident edges survive — insert_node's INSERT OR REPLACE
    # would cascade-drop them.
    new_meta = dict(node.metadata)
    new_meta["deleted"] = True
    new_meta["deleted_at"] = time.time()
    store.update_node_metadata(node.node_id, new_meta)
    console.print(
        f"[green]✓[/green] forgotten [cyan]{node.kind}[/cyan] "
        f"[bold]{node.value}[/bold] [dim]({node.node_id[:8]})[/dim]\n"
        "[dim]soft-delete — the row is kept but hidden from prompts; use "
        "[bold]--hard[/bold] to drop it permanently.[/dim]"
    )


@awareness_app.command("correct")
def correct(
    node_id: Annotated[
        str,
        typer.Argument(help="Node id or unique id prefix of the wrong fact."),
    ],
    new_value: Annotated[
        str,
        typer.Argument(help="The corrected value (quote it if it has spaces)."),
    ],
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Required to correct an identity fact."),
    ] = False,
) -> None:
    """Correct a fact the agent learned wrong.

    Creates a node with the corrected value, records a ``supersedes``
    edge from the new fact to the old one (the provenance the reranker
    will honor), and soft-deletes the old fact so the fix takes effect
    immediately. Identity facts require ``--confirm``.
    """
    from opencomputer.user_model.store import UserModelStore
    from plugin_sdk.user_model import Edge

    store = UserModelStore()
    old = _resolve_node_id(store, node_id)
    console = Console()

    new_value = new_value.strip()
    if not new_value:
        console.print("[bold red]error:[/bold red] the corrected value is empty.")
        raise typer.Exit(1)
    if new_value == old.value:
        console.print(
            f"[dim]{old.value!r} is already the recorded value — "
            "nothing to do.[/dim]"
        )
        return
    if old.kind == "identity" and not confirm:
        console.print(
            f"[bold yellow]refused:[/bold yellow] [cyan]{old.value}[/cyan] is an "
            "[bold]identity[/bold] fact. Correcting it can break later runs.\n"
            "Re-run with [bold]--confirm[/bold] if you are sure."
        )
        raise typer.Exit(1)

    # 1. Materialise the corrected value (same kind) at confidence 1.0 —
    #    an explicit user correction is the most trustworthy signal.
    new_node = store.upsert_node(kind=old.kind, value=new_value, confidence=1.0)
    # 2. Record the supersedes edge new → old. M3/M4 read this to prefer
    #    the correction and demote the superseded fact.
    store.insert_edge(
        Edge(
            kind="supersedes",
            from_node=new_node.node_id,
            to_node=old.node_id,
            salience=1.0,
            confidence=1.0,
            source_reliability=1.0,
            evidence={"corrected_via": "oc awareness correct"},
            source="user_explicit",
        )
    )
    # 3. Soft-delete the old node so the correction is effective now, not
    #    only once the reranker ships. In-place UPDATE (not insert_node)
    #    so the supersedes edge written in step 2 is not cascade-dropped.
    new_meta = dict(old.metadata)
    new_meta["deleted"] = True
    new_meta["deleted_at"] = time.time()
    new_meta["superseded_by"] = new_node.node_id
    store.update_node_metadata(old.node_id, new_meta)

    console.print(
        f"[green]✓[/green] corrected [cyan]{old.kind}[/cyan]\n"
        f"  [dim]was:[/dim] {old.value}\n"
        f"  [dim]now:[/dim] [bold]{new_value}[/bold] "
        f"[dim]({new_node.node_id[:8]})[/dim]"
    )


@awareness_app.command("migrate")
def migrate(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Perform the migration. Without it, only a dry-run plan prints.",
        ),
    ] = False,
) -> None:
    """Clean up legacy user-model graph cruft.

    Two passes: (1) flag agent-internal-noise facts with a
    ``needs_review`` marker — surfaced by ``review --needs-review``;
    (2) collapse the duplicate edges left by the pre-M2 motif importer.

    Dry-run by default — prints the plan and changes nothing. Pass
    ``--apply`` to mutate the graph.
    """
    from opencomputer.user_model.store import UserModelStore
    from plugin_sdk.user_model import NodeKindValidator

    store = UserModelStore()
    console = Console()
    validator = NodeKindValidator()

    # Pass 1 — scan for agent-internal-noise nodes not already flagged
    # or soft-deleted.
    noise: list[tuple[Node, str]] = []
    for n in store.list_nodes(limit=1_000_000):
        if _node_is_deleted(n) or n.metadata.get("needs_review"):
            continue
        verdict = validator.check(n.kind, n.value)
        if not verdict.valid:
            noise.append((n, verdict.reason))

    # Pass 2 — count redundant edges (no mutation in dry-run).
    dup_edges = store.collapse_duplicate_edges(dry_run=True)

    if not apply:
        console.print(
            "[bold]awareness migrate — dry run[/bold] "
            "[dim](no changes made)[/dim]"
        )
        console.print(f"  facts to flag [bold]needs_review[/bold]: {len(noise)}")
        for n, reason in noise[:20]:
            console.print(
                f"    [dim]{n.node_id[:8]}[/dim] ({n.kind}) "
                f"{n.value[:48]} — [dim]{reason}[/dim]"
            )
        if len(noise) > 20:
            console.print(f"    [dim]… and {len(noise) - 20} more[/dim]")
        console.print(f"  duplicate edges to collapse: {dup_edges}")
        console.print(
            "[dim]re-run with [bold]--apply[/bold] to perform the "
            "migration.[/dim]"
        )
        return

    flagged = 0
    for n, reason in noise:
        new_meta = dict(n.metadata)
        new_meta["needs_review"] = True
        new_meta["review_reason"] = reason
        store.update_node_metadata(n.node_id, new_meta)
        flagged += 1
    collapsed = store.collapse_duplicate_edges()
    console.print("[green]✓[/green] awareness migrate applied")
    console.print(f"  facts flagged needs_review: [bold]{flagged}[/bold]")
    console.print(f"  duplicate edges collapsed: [bold]{collapsed}[/bold]")
    if flagged:
        console.print(
            "[dim]flagged facts are excluded from the prompt — inspect them "
            "with [bold]oc awareness review --needs-review[/bold].[/dim]"
        )


@awareness_app.command("eval-ranker")
def eval_ranker(
    query: Annotated[
        str | None,
        typer.Option(
            "--query",
            help="Simulate a session opening message (drives the BM25 term).",
        ),
    ] = None,
    top_k: Annotated[
        int,
        typer.Option("--top-k", help="How many ranked facts to compare."),
    ] = 15,
) -> None:
    """Compare the context-aware reranker against the old static sort.

    Renders the old ``(kind, confidence)`` top-K beside the reranker's
    top-K so the weights can be sanity-checked. ``--query`` simulates the
    opening message that drives the BM25 relevance term.
    """
    from opencomputer.user_model.reranker import (
        SessionContext,
        UserFactsReranker,
    )
    from opencomputer.user_model.store import UserModelStore

    store = UserModelStore()
    console = Console()
    nodes = [
        n
        for n in store.list_nodes(limit=500)
        if not _node_is_deleted(n) and not n.metadata.get("needs_review")
    ]
    if not nodes:
        console.print("[dim]no facts to rank[/dim]")
        return

    old = sorted(
        nodes, key=lambda n: (_KIND_ORDER.get(n.kind, 99), -n.confidence)
    )[:top_k]
    ctx = SessionContext(recent_messages=(query,) if query else ())
    new = [sf.node for sf in UserFactsReranker().score(nodes, ctx)[:top_k]]

    label = f"query={query!r}" if query else "context-free"
    table = Table(title=f"ranker comparison — top {top_k} ({label})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("old — kind + confidence")
    table.add_column("new — context-aware reranker")
    for i in range(max(len(old), len(new))):
        o = old[i].value[:38] if i < len(old) else ""
        n = new[i].value[:38] if i < len(new) else ""
        mark = "" if o == n else "  [yellow]●[/yellow]"
        table.add_row(str(i + 1), o, n + mark)
    console.print(table)

    changed = sum(
        1
        for i in range(min(len(old), len(new)))
        if old[i].node_id != new[i].node_id
    )
    console.print(
        f"[dim]{changed} of {min(len(old), len(new))} positions changed "
        "between the two rankings.[/dim]"
    )
