"""Implementations of ``opencomputer evolution …`` subcommands.

Provider resolution note (B2):
  ``_resolve_provider()`` queries the module-level ``registry`` singleton from
  ``opencomputer.plugins.registry``, which exposes a ``providers`` dict keyed
  by provider name.  This mirrors the pattern used by ``cli._resolve_provider``
  (which also calls ``plugin_registry.providers.get(provider_name)``).  We do
  NOT import from ``opencomputer.cli`` because that file is Session A's
  reserved file; instead we look up the same global registry object directly.
  If the registry has no providers loaded (typical in a freshly spawned CLI
  with no provider plugin enabled), we raise a ``RuntimeError`` with an
  actionable message.
"""

from __future__ import annotations

import shutil

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.evolution.entrypoint import evolution_app
from opencomputer.evolution.reflect import ReflectionEngine
from opencomputer.evolution.storage import (
    evolution_home,
    init_db,
    list_recent,
)
from opencomputer.evolution.synthesize import SkillSynthesizer

console = Console()


# ---------------------------------------------------------------------------
# skills sub-group
# ---------------------------------------------------------------------------

skills_app = typer.Typer(
    name="skills",
    help="Manage synthesized skills (the evolution quarantine namespace).",
    no_args_is_help=True,
)
evolution_app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list() -> None:
    """Show synthesized skills currently in the evolution quarantine."""
    skills_dir = evolution_home() / "skills"
    if not skills_dir.exists():
        console.print("[dim]No synthesized skills yet.[/dim]")
        return
    rows = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        # Read description from frontmatter (first lines, name: / description:)
        description = ""
        for line in skill_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
                break
            if line.startswith("---") and rows:  # second --- = end of frontmatter
                break
        rows.append((child.name, description))
    if not rows:
        console.print("[dim]No synthesized skills yet.[/dim]")
        return
    table = Table(title="Synthesized skills (evolution quarantine)")
    table.add_column("slug", style="cyan")
    table.add_column("description")
    for slug, desc in rows:
        table.add_row(slug, desc)
    console.print(table)


@skills_app.command("promote")
def skills_promote(
    slug: str = typer.Argument(..., help="Slug of synthesized skill to promote"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing main-skills entry"),
) -> None:
    """Copy a synthesized skill from the evolution quarantine to the user's main skills dir."""
    src = evolution_home() / "skills" / slug
    if not src.exists():
        console.print(f"[red]Synthesized skill not found:[/red] {src}")
        raise typer.Exit(code=1)
    # Main skills dir per existing convention (_home() / "skills") — see agent/config.py
    from opencomputer.agent.config import (
        _home as _profile_home,  # local import to avoid load order issues
    )

    main_dir = _profile_home() / "skills" / slug
    if main_dir.exists() and not force:
        console.print(
            f"[red]Main skill already exists:[/red] {main_dir}\n"
            "[dim]Use --force to overwrite.[/dim]"
        )
        raise typer.Exit(code=1)
    if main_dir.exists() and force:
        shutil.rmtree(main_dir)
    main_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, main_dir)
    console.print(f"[green]Promoted[/green] {slug} → {main_dir}")


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@evolution_app.command("reflect")
def reflect(
    window: int = typer.Option(
        30,
        "--window",
        help="Number of recent trajectories to reflect on (default 30)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Render the prompt + show counts without calling the LLM or synthesizing",
    ),
    model: str = typer.Option(
        "claude-opus-4-7",
        "--model",
        help="Model to use for reflection (provider must be configured)",
    ),
) -> None:
    """Manually trigger a reflection pass on recent trajectories."""
    conn = init_db()
    records = list_recent(limit=window, conn=conn)
    if not records:
        console.print("[dim]No trajectories to reflect on. Auto-collection lands in B3.[/dim]")
        return
    console.print(
        f"Reflecting on {len(records)} trajectories (window={window}, model={model})..."
    )
    if dry_run:
        # Show summary; do NOT call provider
        table = Table(title="Trajectories to reflect on")
        table.add_column("id")
        table.add_column("session_id")
        table.add_column("events")
        table.add_column("completion")
        for r in records:
            table.add_row(
                str(r.id),
                r.session_id,
                str(len(r.events)),
                "✓" if r.completion_flag else "✗",
            )
        console.print(table)
        console.print("[yellow]Dry-run: no LLM call made.[/yellow]")
        return
    # Real reflection requires a provider. For B2, raise an actionable error if none.
    try:
        provider = _resolve_provider()
    except RuntimeError as exc:
        console.print(f"[red]Cannot resolve provider:[/red] {exc}")
        raise typer.Exit(code=2)
    engine = ReflectionEngine(provider=provider, model=model, window=window)
    insights = engine.reflect(records)
    console.print(f"[green]Got {len(insights)} insights.[/green]")
    synth = SkillSynthesizer()
    created = []
    for ins in insights:
        if ins.action_type == "create_skill":
            try:
                path = synth.synthesize(ins)
                created.append(path)
                console.print(f"  [cyan]synthesized[/cyan] {path}")
            except (ValueError, FileExistsError) as exc:
                console.print(f"  [yellow]skipped insight:[/yellow] {exc}")
    console.print(f"[bold]Synthesized {len(created)} skills.[/bold]")


@evolution_app.command("reset")
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete all evolution data: DB + synthesized skills + (future) prompt proposals.

    Your sessions DB and main skills are NOT touched.
    """
    # Compute path WITHOUT calling evolution_home() so we don't create the dir
    # just to check if it exists (evolution_home() has a mkdir side-effect).
    from opencomputer.agent.config import _home as _profile_home

    eh = _profile_home() / "evolution"
    if not eh.exists():
        console.print("[dim]No evolution data to delete.[/dim]")
        return
    if not yes:
        confirm = typer.confirm(f"Delete entire evolution dir at {eh}?")
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(code=0)
    shutil.rmtree(eh)
    console.print(f"[green]Deleted[/green] {eh}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_provider():
    """Return a BaseProvider instance, or raise RuntimeError with actionable message.

    Provider resolution strategy (B2 MVP):
      We query the module-level ``registry`` singleton from
      ``opencomputer.plugins.registry``, which holds a ``providers`` dict
      keyed by provider-name strings (populated by ``registry.load_all()``
      at CLI startup).  We return the first registered provider.

      This mirrors the exact approach used by ``opencomputer.cli._resolve_provider``
      — which also calls ``plugin_registry.providers.get(provider_name)`` — but
      adapted for the evolution CLI which (a) doesn't know the configured
      provider name at import time, and (b) must not import from cli.py.

      If the registry is empty (provider plugins not loaded), we raise a clear
      error so the user knows what to do.
    """
    # Local import — keeps the CLI surface independent of plugin-registry load order.
    try:
        from opencomputer.plugins.registry import registry  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "plugin registry not importable — ensure opencomputer is installed correctly"
        ) from exc

    providers = registry.providers  # dict[str, BaseProvider | type[BaseProvider]]
    if not providers:
        raise RuntimeError(
            "No provider plugin enabled. "
            "Run `opencomputer plugin enable anthropic-provider` "
            "(or another provider) first."
        )
    # First provider wins for B2 MVP — user can configure preference later.
    _first = next(iter(providers.values()))
    # Plugins may register the class OR an instance; handle both.
    return _first() if isinstance(_first, type) else _first


__all__ = [
    "skills_list",
    "skills_promote",
    "reflect",
    "reset",
]
