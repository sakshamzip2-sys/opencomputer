"""``oc rules`` CLI — inspect path-glob rules.

v1.1 plan-2 M7.2 (2026-05-09). Surfaces the
``opencomputer.agent.rules_loader`` data so operators can answer "why
isn't my rule firing on src/foo.tsx?" without grepping the agent's
log output.

Subcommands:

* ``oc rules list`` — every rule loaded from workspace + active profile.
* ``oc rules check <path>`` — rules whose globs match ``path``.
* ``oc rules show <name>`` — full body + frontmatter of one rule.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

rules_app = typer.Typer(
    name="rules",
    help="Inspect path-glob rules loaded from .opencomputer/rules/*.md.",
)
console = Console()


def _resolve_dirs() -> tuple[Path, Path]:
    """Return (workspace_rules_dir, profile_rules_dir) — both may not exist."""
    from opencomputer.agent.config import _home

    workspace = Path.cwd() / ".opencomputer" / "rules"
    profile = _home() / "rules"
    return workspace, profile


def _load_merged():
    from opencomputer.agent.rules_loader import merged_rules

    workspace, profile = _resolve_dirs()
    return merged_rules(workspace, profile), workspace, profile


@rules_app.command("list")
def rules_list(
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON."
    ),
) -> None:
    """List all path-glob rules visible from workspace + active profile."""
    rules, workspace, profile = _load_merged()

    if json_out:
        typer.echo(
            json.dumps(
                {
                    "workspace_dir": str(workspace),
                    "profile_dir": str(profile),
                    "rules": [
                        {
                            "name": r.name,
                            "paths": list(r.paths),
                            "priority": r.priority,
                            "source": str(r.source),
                            "body_chars": len(r.body),
                        }
                        for r in rules
                    ],
                }
            )
        )
        return

    if not rules:
        console.print(
            "[yellow]no rules loaded[/yellow] — drop ``*.md`` files into "
            f"{workspace} or {profile} with a ``paths:`` frontmatter list."
        )
        return

    table = Table(title="Path-glob rules", show_lines=False)
    table.add_column("name", style="cyan")
    table.add_column("paths")
    table.add_column("priority", justify="right")
    table.add_column("source", style="dim")
    for rule in rules:
        rel_source = _relativize(rule.source, workspace, profile)
        table.add_row(
            rule.name,
            ", ".join(rule.paths) or "[red](none — never matches)[/red]",
            str(rule.priority),
            rel_source,
        )
    console.print(table)
    console.print(
        f"[dim]workspace dir: {workspace} (exists={workspace.exists()})[/dim]"
    )
    console.print(
        f"[dim]profile dir:   {profile} (exists={profile.exists()})[/dim]"
    )


@rules_app.command("check")
def rules_check(
    path: str = typer.Argument(..., help="A file path to test against rule globs."),
    json_out: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON."
    ),
) -> None:
    """Show which loaded rules match ``path``.

    Uses the same fnmatch matcher the live agent uses, so a green
    "matches" answer here means the rule WILL fire on the next
    path-touching tool call against ``path``.
    """
    from opencomputer.agent.rules_loader import active_rules_for

    rules, _workspace, _profile = _load_merged()
    matched = active_rules_for(rules, [path])

    if json_out:
        typer.echo(
            json.dumps(
                {
                    "path": path,
                    "matched": [
                        {
                            "name": r.name,
                            "paths": list(r.paths),
                            "priority": r.priority,
                        }
                        for r in matched
                    ],
                }
            )
        )
        return

    if not matched:
        console.print(
            f"[yellow]no rules match[/yellow] [cyan]{path}[/cyan]. "
            f"Loaded: {len(rules)} total."
        )
        return

    console.print(
        f"[green]{len(matched)} rule(s) match[/green] [cyan]{path}[/cyan]:"
    )
    for rule in matched:
        console.print(
            f"  • [bold]{rule.name}[/bold] "
            f"(priority {rule.priority}, paths: {', '.join(rule.paths)})"
        )


@rules_app.command("show")
def rules_show(
    name: str = typer.Argument(..., help="Rule name (filename stem)."),
) -> None:
    """Print one rule's body + frontmatter."""
    rules, _w, _p = _load_merged()
    target = next((r for r in rules if r.name == name), None)
    if target is None:
        console.print(
            f"[red]error:[/red] no rule named [cyan]{name}[/cyan]. "
            f"Run [bold]oc rules list[/bold] to see what's loaded."
        )
        raise typer.Exit(1)

    console.print(f"[bold cyan]{target.name}[/bold cyan] — {target.source}")
    console.print(
        f"[dim]paths: {', '.join(target.paths)} | priority: {target.priority}[/dim]"
    )
    console.print()
    if target.body:
        try:
            console.print(Syntax(target.body, "markdown", word_wrap=True))
        except Exception:  # noqa: BLE001 — fall back to plain print
            console.print(target.body)
    else:
        console.print("[dim](rule body is empty)[/dim]")


def _relativize(source: Path, workspace: Path, profile: Path) -> str:
    """Render rule source as ``workspace:name`` or ``profile:name`` for compactness."""
    try:
        rel = source.relative_to(workspace)
        return f"workspace:{rel}"
    except ValueError:
        pass
    try:
        rel = source.relative_to(profile)
        return f"profile:{rel}"
    except ValueError:
        pass
    return str(source)


__all__ = ["rules_app"]
