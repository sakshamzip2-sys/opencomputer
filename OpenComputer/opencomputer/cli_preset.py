"""``opencomputer preset`` — manage named plugin-activation presets (Phase 14.M).

Subcommands:
    create <name> --plugins a,b,c [--force]
    list
    show <name>
    edit <name>
    delete <name> [--yes]
    where [<name>]

Preset files live at ``~/.opencomputer/presets/<name>.yaml`` and are
shared across profiles. See ``opencomputer.plugins.preset`` for the
underlying model + I/O helpers.
"""

from __future__ import annotations

import os
import subprocess

import typer
from rich.console import Console

from opencomputer.plugins.preset import (
    list_presets,
    load_preset,
    preset_path,
    presets_dir,
    write_preset,
)

preset_app = typer.Typer(
    name="preset",
    help="Manage named plugin-activation presets.",
    no_args_is_help=True,
)
_console = Console()


@preset_app.command("create")
def create_cmd(
    name: str = typer.Argument(..., help="Preset name (kebab-case)."),
    plugins: str = typer.Option(
        ...,
        "--plugins",
        help="Comma-separated plugin ids.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing preset.",
    ),
) -> None:
    """Write a new preset to ``~/.opencomputer/presets/<name>.yaml``."""
    ids = [p.strip() for p in plugins.split(",") if p.strip()]
    try:
        path = write_preset(name, ids, overwrite=force)
    except FileExistsError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    except ValueError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    _console.print(
        f"[green]created[/green] preset [bold]{name}[/bold] with {len(ids)} plugin(s) -> {path}"
    )


@preset_app.command("list")
def list_cmd() -> None:
    """List all presets in ``~/.opencomputer/presets/``."""
    names = list_presets()
    if not names:
        _console.print("(no presets)")
        return
    for n in names:
        _console.print(n)


@preset_app.command("show")
def show_cmd(
    name: str = typer.Argument(..., help="Preset name."),
) -> None:
    """Print the plugins listed in a preset."""
    try:
        p = load_preset(name)
    except FileNotFoundError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    _console.print(f"[bold]preset:[/bold] {name}")
    if not p.plugins:
        _console.print("  (empty — no plugins)")
        return
    for pid in p.plugins:
        _console.print(f"  - {pid}")


@preset_app.command("edit")
def edit_cmd(
    name: str = typer.Argument(..., help="Preset name."),
) -> None:
    """Open a preset file in ``$EDITOR`` (defaults to ``vi``)."""
    path = preset_path(name)
    if not path.exists():
        _console.print(f"[red]error:[/red] preset {name!r} does not exist")
        raise typer.Exit(code=1)
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(path)], check=False)


@preset_app.command("delete")
def delete_cmd(
    name: str = typer.Argument(..., help="Preset name."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt.",
    ),
) -> None:
    """Remove a preset file.

    NOTE (Phase 14.M integration): once zesty 14.D lands, this command
    will also warn when a profile currently references the preset being
    deleted. Until then, caveat: deleting a referenced preset makes the
    profile fail to resolve until the reference is fixed.
    """
    path = preset_path(name)
    if not path.exists():
        _console.print(f"[red]error:[/red] preset {name!r} does not exist")
        raise typer.Exit(code=1)
    if not yes:
        confirm = typer.confirm(f"delete preset {name!r} at {path}?", default=False)
        if not confirm:
            _console.print("aborted")
            raise typer.Exit(code=1)
    path.unlink()
    _console.print(f"[green]deleted[/green] preset {name}")


@preset_app.command("where")
def where_cmd(
    name: str | None = typer.Argument(
        None,
        help="Preset name. Omit to print the presets directory.",
    ),
) -> None:
    """Print the filesystem path of a preset (or the presets dir)."""
    if name is None:
        _console.print(str(presets_dir()))
        return
    _console.print(str(preset_path(name)))


__all__ = ["preset_app"]
