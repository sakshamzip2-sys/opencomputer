"""``oc favorites`` — manage the scoped-models short list.

The list is read at runtime by
:mod:`opencomputer.cli_ui._model_swap` to power the Alt+M keybinding.
This CLI gives the user a real surface (vs hand-editing YAML) to
``add`` / ``list`` / ``remove`` favorite model ids per profile.

Storage: ``<profile_dir>/favorites.yaml``::

    models:
      - claude-opus-4-7
      - claude-sonnet-4-6
      - claude-haiku-4-5-20251001

Writes go through ``filelock.FileLock`` so concurrent ``oc favorites add``
calls don't lose entries — matches the bindings.yaml / profile.yaml
pattern.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
import yaml
from filelock import FileLock
from rich.console import Console
from rich.table import Table

from opencomputer.profiles import get_profile_dir, read_active_profile

_log = logging.getLogger(__name__)

favorites_app = typer.Typer(
    name="favorites",
    help="Manage scoped-models favorites for Alt+M cycling.",
    no_args_is_help=True,
)


def _favorites_path(profile_id: str | None = None) -> Path:
    """Resolve the favorites.yaml path for a profile.

    Falls back to the currently-active profile when ``profile_id`` is
    ``None``. Matches :func:`opencomputer.cli_ui._model_swap._favorites_path`."""
    if profile_id is None:
        profile_id = read_active_profile()
    return get_profile_dir(profile_id) / "favorites.yaml"


def _lock_path(favorites: Path) -> Path:
    """Sibling lock file. We never lock the favorites.yaml itself
    because filelock + Python typing issues with file_descriptors on
    macOS make the sibling file safer — same pattern as bindings.yaml."""
    return favorites.with_suffix(favorites.suffix + ".lock")


def _load(profile_id: str | None = None) -> list[str]:
    """Read the current favorites. Returns empty list when file is
    missing or malformed — never raises."""
    p = _favorites_path(profile_id)
    if not p.exists():
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        _log.warning("favorites.yaml unreadable at %s: %s", p, exc)
        return []
    if not isinstance(raw, dict):
        return []
    models = raw.get("models")
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, str) and m.strip()]


def _save(models: list[str], profile_id: str | None = None) -> None:
    """Atomic-ish write under flock. Writes the full YAML doc; on
    success the parent dir is guaranteed to exist."""
    p = _favorites_path(profile_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(_lock_path(p)))
    with lock:
        # Write to a sibling tmp then rename for crash safety.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump({"models": models}, sort_keys=False))
        tmp.replace(p)


@favorites_app.command("list")
def list_cmd() -> None:
    """Show the current favorites for the active profile."""
    models = _load()
    console = Console()
    if not models:
        console.print(
            "[dim]no favorites — add some with:[/dim]  "
            "[cyan]oc favorites add <model-id>[/cyan]"
        )
        return
    table = Table(
        title=f"scoped-models favorites ({len(models)})",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("model id")
    for i, m in enumerate(models, 1):
        table.add_row(str(i), m)
    console.print(table)


@favorites_app.command("add")
def add_cmd(
    model_id: str = typer.Argument(
        ...,
        help="Model identifier as you'd pass to /model. Whitespace is stripped.",
    ),
) -> None:
    """Append a model id to the favorites list.

    Validation: non-empty, non-duplicate. Writes are flock'd so
    concurrent calls don't lose entries.
    """
    model_id = model_id.strip()
    if not model_id:
        typer.echo("error: model id is required (got empty string)", err=True)
        raise typer.Exit(code=2)
    current = _load()
    if model_id in current:
        typer.echo(f"error: {model_id!r} is already in favorites", err=True)
        raise typer.Exit(code=1)
    current.append(model_id)
    try:
        _save(current)
    except OSError as exc:
        typer.echo(f"error: failed to write favorites.yaml: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    Console().print(
        f"[green]✓[/green] added [cyan]{model_id}[/cyan] "
        f"([dim]{len(current)} total[/dim])"
    )


@favorites_app.command("remove")
def remove_cmd(
    model_id: str = typer.Argument(..., help="Model identifier to remove."),
) -> None:
    """Remove a model id from the favorites list.

    Missing entries are a no-op-with-warning, not an error — running
    ``oc favorites remove X`` twice should still exit clean.
    """
    model_id = model_id.strip()
    current = _load()
    if model_id not in current:
        Console().print(
            f"[yellow]⚠[/yellow] [cyan]{model_id}[/cyan] not in favorites"
        )
        return
    current.remove(model_id)
    try:
        _save(current)
    except OSError as exc:
        typer.echo(f"error: failed to write favorites.yaml: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    Console().print(
        f"[green]✓[/green] removed [cyan]{model_id}[/cyan] "
        f"([dim]{len(current)} remaining[/dim])"
    )


@favorites_app.command("path")
def path_cmd() -> None:
    """Print the resolved favorites.yaml path for the active profile.
    Useful when debugging or scripting."""
    typer.echo(str(_favorites_path()))


__all__ = ["favorites_app"]
