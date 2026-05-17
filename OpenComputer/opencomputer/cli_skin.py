"""``oc skin`` subcommand — list / set / preview CLI skins.

Best-of-three Recipe 4, Part C. The skin *engine* (``cli_ui/skin/``),
the 9 built-in skins, user skins, apply-at-boot, and the in-chat
``/skin`` slash command all already shipped (Hermes v2, PR #515). The
only gap was a top-level CLI so a skin can be inspected or switched
without entering a chat session — scriptable, and visible before the
first ``oc chat``.

All three commands reuse the existing engine:
- ``list``    → :func:`list_builtin_names` + a scan of ``USER_SKINS_DIR``
- ``set``     → :func:`set_display_skin` (persists to the profile config)
- ``preview`` → :func:`load_skin` + a Rich swatch render
"""
from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

skin_app = typer.Typer(help="Inspect and switch the CLI colour skin.")

_console = Console()


def _config_path():  # type: ignore[no-untyped-def]
    """Active profile's ``config.yaml`` — resolved at call time so tests
    and ``oc -p`` profile switches see the right file."""
    from opencomputer.agent.config import _home

    return _home() / "config.yaml"


def _current_skin() -> str:
    from opencomputer.agent.profile_yaml import get_display_skin

    try:
        return get_display_skin(_config_path()) or "default"
    except Exception:  # noqa: BLE001 — a broken config must not break listing
        return "default"


def _user_skin_names() -> list[str]:
    from opencomputer.cli_ui.skin import USER_SKINS_DIR

    if not USER_SKINS_DIR.is_dir():
        return []
    return sorted(p.stem for p in USER_SKINS_DIR.glob("*.yaml"))


@skin_app.command("list")
def list_skins() -> None:
    """List every available skin and mark the active one."""
    from opencomputer.cli_ui.skin import list_builtin_names

    current = _current_skin()
    builtins = list_builtin_names()
    user = [n for n in _user_skin_names() if n not in builtins]

    table = Table(title="Skins", show_header=True, header_style="bold")
    table.add_column("", style="green", width=2)
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="dim")
    for name in builtins:
        mark = "●" if name == current else ""
        table.add_row(mark, name, "built-in")
    for name in user:
        mark = "●" if name == current else ""
        table.add_row(mark, name, "user")
    _console.print(table)
    _console.print(f"[dim]active skin: [/dim][cyan]{current}[/cyan]")


@skin_app.command("set")
def set_skin(
    name: str = typer.Argument(..., help="Skin name to activate."),
) -> None:
    """Set the active skin (persisted to the profile config)."""
    from opencomputer.agent.profile_yaml import set_display_skin
    from opencomputer.cli_ui.skin import list_builtin_names, load_skin

    requested = name.strip().lower()
    # load_skin falls back to "default" with a warning when a name
    # resolves to nothing — a resolved name mismatch means a typo.
    spec = load_skin(requested)
    if spec.name != requested:
        available = ", ".join(list_builtin_names() + _user_skin_names())
        _console.print(
            f"[red]unknown skin:[/red] {requested!r}\n"
            f"[dim]available:[/dim] {available}\n"
            f"[dim]drop custom YAML at "
            f"~/.opencomputer/skins/{requested}.yaml[/dim]"
        )
        raise typer.Exit(code=1)
    try:
        set_display_skin(_config_path(), requested)
    except OSError as exc:
        _console.print(f"[red]could not persist skin:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    _console.print(
        f"[green]skin set to[/green] [cyan]{requested}[/cyan] "
        f"[dim](applies on next oc chat; use /skin to repaint live)[/dim]"
    )


@skin_app.command("preview")
def preview_skin(
    name: str = typer.Argument(
        "", help="Skin to preview. Defaults to the active skin."
    ),
) -> None:
    """Render the skin's colour palette as labelled swatches."""
    from opencomputer.cli_ui.skin import load_skin

    target = name.strip().lower() or _current_skin()
    spec = load_skin(target)
    if not spec.colors:
        _console.print(f"[yellow]skin {spec.name!r} declares no colours[/yellow]")
        return
    table = Table(
        title=f"Skin preview — {spec.name}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Token", style="cyan")
    table.add_column("Hex", style="dim")
    table.add_column("Swatch")
    for token, hex_value in spec.colors.items():
        try:
            swatch = f"[{hex_value}]████████[/{hex_value}]"
        except Exception:  # noqa: BLE001 — a bad hex must not abort the table
            swatch = "[dim]?[/dim]"
        table.add_row(token, str(hex_value), swatch)
    _console.print(table)
    if spec.description:
        _console.print(f"[dim]{spec.description}[/dim]")


__all__ = ["skin_app"]
