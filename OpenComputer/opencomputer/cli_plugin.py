"""Phase 14.E — `opencomputer plugin` CLI subcommand group.

Manages installation of user-authored plugins into either the profile-local
plugin dir (``~/.opencomputer/profiles/<name>/plugins/``) or the global
shared dir (``~/.opencomputer/plugins/``). The singular `plugin` sub-app
complements the existing plural `plugins` command (which lists).

Commands:

  opencomputer plugin install <path> [--profile X] [--global]
    Install a plugin directory into the chosen location. Defaults to
    ``--profile <active>`` (profile-local) so installs don't pollute
    global by accident. Source must contain a ``plugin.json``.

  opencomputer plugin uninstall <id> [--profile X] [--global]
    Remove a plugin by id from the chosen location. Refuses to touch
    bundled ``extensions/*`` plugins.

  opencomputer plugin where <id>
    Print the filesystem path of an installed plugin (searches all
    known roots: profile-local → global → bundled).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console

plugin_app = typer.Typer(
    name="plugin",
    help="Manage installed plugins (install/uninstall/where).",
    no_args_is_help=True,
)
_console = Console()


def _resolve_destination_root(profile: str | None, is_global: bool) -> Path:
    """Where does the install go? Based on --profile vs --global flags.

    Precedence:
      - --global → ``~/.opencomputer/plugins/``.
      - --profile <name> → ``~/.opencomputer/profiles/<name>/plugins/``.
      - Neither → active profile's plugin dir (or global if default).
    """
    from opencomputer.profiles import (
        ProfileNameError,
        get_default_root,
        get_profile_dir,
        read_active_profile,
    )

    if is_global:
        return get_default_root() / "plugins"
    if profile:
        try:
            return get_profile_dir(profile) / "plugins"
        except ProfileNameError as e:
            _console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(code=1) from None
    # Default: active profile (falls back to global root for default profile)
    active = read_active_profile()
    if active is None:
        return get_default_root() / "plugins"
    return get_profile_dir(active) / "plugins"


def _load_source_manifest(src: Path) -> dict:
    import json

    manifest_path = src / "plugin.json"
    if not manifest_path.exists():
        _console.print(
            f"[red]error:[/red] source dir {src} has no plugin.json (is this a plugin directory?)"
        )
        raise typer.Exit(code=1)
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        _console.print(f"[red]error:[/red] failed to parse {manifest_path}: {e}")
        raise typer.Exit(code=1) from None


@plugin_app.command("install")
def install(
    source: Path = typer.Argument(
        ...,
        help="Path to the plugin directory (must contain plugin.json).",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Install into a specific profile's local plugin dir.",
    ),
    is_global: bool = typer.Option(
        False,
        "--global",
        help="Install globally (shared across profiles). Overrides --profile.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite if a plugin with the same id already exists.",
    ),
) -> None:
    """Install a plugin directory into the profile or global location."""
    manifest = _load_source_manifest(source)
    plugin_id = manifest.get("id")
    if not plugin_id:
        _console.print("[red]error:[/red] plugin.json missing required 'id' field")
        raise typer.Exit(code=1)

    dest_root = _resolve_destination_root(profile, is_global)
    dest = dest_root / plugin_id

    if dest.exists():
        if not force:
            _console.print(
                f"[red]error:[/red] plugin '{plugin_id}' already exists at {dest}. "
                "Use --force to overwrite."
            )
            raise typer.Exit(code=1)
        shutil.rmtree(dest)

    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
    _console.print(f"[green]installed:[/green] '{plugin_id}' → {dest}")


@plugin_app.command("uninstall")
def uninstall(
    plugin_id: str = typer.Argument(..., help="Plugin id to remove."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Uninstall from a specific profile's local dir.",
    ),
    is_global: bool = typer.Option(
        False,
        "--global",
        help="Uninstall from the global shared dir.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove an installed plugin from the chosen location."""
    dest_root = _resolve_destination_root(profile, is_global)
    target = dest_root / plugin_id

    if not target.exists():
        _console.print(f"[red]error:[/red] plugin '{plugin_id}' not found at {target}")
        raise typer.Exit(code=1)

    if not yes:
        confirm = typer.confirm(f"Remove plugin '{plugin_id}' at {target}?")
        if not confirm:
            _console.print("aborted.")
            raise typer.Exit()

    shutil.rmtree(target)
    _console.print(f"[green]uninstalled:[/green] '{plugin_id}' from {target}")


@plugin_app.command("where")
def where(
    plugin_id: str = typer.Argument(..., help="Plugin id to locate."),
) -> None:
    """Print the filesystem path of an installed plugin.

    Searches in priority order: profile-local → global → bundled
    ``extensions/``. Prints the first match.
    """
    from opencomputer.profiles import (
        get_default_root,
        get_profile_dir,
        read_active_profile,
    )

    search: list[Path] = []

    active = read_active_profile()
    if active is not None:
        # Don't rely on _home() here — it reads OPENCOMPUTER_HOME which is
        # set by _apply_profile_override inside main(); we might be invoked
        # in a context where main() hasn't run (tests, programmatic).
        search.append(get_profile_dir(active) / "plugins")
    search.append(get_default_root() / "plugins")
    repo_root = Path(__file__).resolve().parent.parent
    search.append(repo_root / "extensions")

    for root in search:
        candidate = root / plugin_id
        if candidate.is_dir() and (candidate / "plugin.json").exists():
            typer.echo(str(candidate))
            return

    _console.print(f"[red]error:[/red] plugin '{plugin_id}' not found")
    raise typer.Exit(code=1)


__all__ = ["plugin_app"]
