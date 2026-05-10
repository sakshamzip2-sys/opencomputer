"""``oc parity-doctor`` CLI — print or write the upstream-parity table."""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.parity_doctor import (
    _STATUS_ICON,
    render_markdown,
    run_checks,
)

parity_app = typer.Typer(help="Compare OC against an upstream reference spec.")
_console = Console()


def _default_spec_path() -> Path:
    """Walk up from CWD to find ``docs/OC-FROM-OPENCLAW.md``.

    Returns the first match, falling back to the well-known location
    when run from inside the repo.
    """
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "docs" / "OC-FROM-OPENCLAW.md"
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parents[1] / "docs" / "OC-FROM-OPENCLAW.md"


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@parity_app.command("run")
def cmd_run(
    spec: Path = typer.Option(
        None,
        "--spec",
        help="Path to the parity spec markdown. Defaults to docs/OC-FROM-OPENCLAW.md.",
    ),
    repo_root: Path = typer.Option(
        None,
        "--repo-root",
        help="Search root for symbols. Defaults to the OC repo root.",
    ),
    write: Path = typer.Option(
        None,
        "--write",
        help="Write the Markdown report to this path (in addition to stdout).",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit results as JSON instead of a table.",
    ),
) -> None:
    """Run all parity checks and print a status table."""
    spec_path = (spec or _default_spec_path()).resolve()
    root_path = (repo_root or _default_repo_root()).resolve()
    if not spec_path.is_file():
        _console.print(f"[red]Spec not found:[/red] {spec_path}")
        raise typer.Exit(code=2)

    results = run_checks(spec_path=spec_path, repo_root=root_path)

    if json_out:

        payload = [
            {
                "number": r.record.number,
                "tier": r.record.tier,
                "title": r.record.title,
                "status": r.status,
                "matched": list(r.matched),
                "missing": list(r.missing),
                "notes": r.notes,
            }
            for r in results
        ]
        _console.print_json(data=payload)
    else:
        table = Table(title=f"Parity vs {spec_path.name}")
        table.add_column("#", justify="right")
        table.add_column("Tier", justify="right")
        table.add_column("Status")
        table.add_column("Feature")
        table.add_column("Notes")
        for r in results:
            icon = _STATUS_ICON[r.status]
            colour = {
                "shipped": "green",
                "partial": "yellow",
                "scaffolded": "orange3",
                "missing": "red",
            }.get(r.status, "white")
            table.add_row(
                str(r.record.number),
                str(r.record.tier),
                f"[{colour}]{icon} {r.status}[/{colour}]",
                r.record.title,
                r.notes,
            )
        _console.print(table)

    if write:
        md = render_markdown(results)
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(md, encoding="utf-8")
        _console.print(f"[green]wrote[/green] {write}")

    # Non-zero exit when EVERY tier-1 feature is not ``shipped`` — the
    # CI gate. Lower tiers are informational.
    tier1 = [r for r in results if r.record.tier == 1]
    bad_tier1 = [r for r in tier1 if r.status not in ("shipped",)]
    if bad_tier1:
        # Match doctor convention: warn-level exit (1) when something
        # could be improved. Operator can pipe to `|| true` if they
        # don't want CI to fail on partial states.
        if "--ci" in sys.argv:
            raise typer.Exit(code=1)


@parity_app.command("list-checks")
def cmd_list_checks() -> None:
    """List the registered feature checks (without running them)."""
    from opencomputer.parity_doctor import FEATURE_CHECKS

    table = Table(title="Registered parity checks")
    table.add_column("#", justify="right")
    table.add_column("Feature")
    table.add_column("Symbols")
    for c in FEATURE_CHECKS:
        table.add_row(str(c.number), c.title, ", ".join(c.symbols))
    _console.print(table)
