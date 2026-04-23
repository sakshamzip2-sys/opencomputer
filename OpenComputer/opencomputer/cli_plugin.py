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
import sys
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


_VALID_KINDS: tuple[str, ...] = ("channel", "provider", "toolkit", "mixed")


def _smoke_load_plugin(plugin_dir: Path) -> None:
    """Run a post-scaffold smoke check: does the rendered plugin load?

    Builds an ISOLATED PluginRegistry (NOT the process-global one) so
    scaffolding a plugin never pollutes the running agent's
    tool/provider/channel tables. Raises on any failure (invalid
    manifest, bad entry module, register() error) — the caller converts
    the exception into a red status line + exit 1.

    Also scrubs sys.modules + sys.path entries this load contributed
    so that scaffolding multiple plugins in one process (tests, batch
    scripts) doesn't leak cached sibling modules like ``tools.my_tool``
    from run N into run N+1.
    """
    from opencomputer.plugins.discovery import PluginCandidate, _parse_manifest
    from opencomputer.plugins.loader import load_plugin
    from opencomputer.plugins.registry import PluginRegistry

    manifest_path = plugin_dir / "plugin.json"
    manifest = _parse_manifest(manifest_path)
    if manifest is None:
        raise RuntimeError(
            f"rendered plugin.json at {manifest_path} is invalid or unparseable"
        )

    candidate = PluginCandidate(
        manifest=manifest,
        root_dir=plugin_dir,
        manifest_path=manifest_path,
    )

    # Isolated registry — separate tool_registry/hook_engine too so nothing
    # leaks into the running process. We build a fresh PluginRegistry and
    # use a bespoke PluginAPI that points at throwaway registries.
    from opencomputer.hooks.engine import HookEngine
    from opencomputer.plugins.loader import PluginAPI
    from opencomputer.tools.registry import ToolRegistry

    isolated_tools = ToolRegistry()
    isolated_hooks = HookEngine()
    isolated_registry = PluginRegistry()
    api = PluginAPI(
        tool_registry=isolated_tools,
        hook_engine=isolated_hooks,
        provider_registry=isolated_registry.providers,
        channel_registry=isolated_registry.channels,
        injection_engine=None,
        doctor_contributions=isolated_registry.doctor_contributions,
    )

    plugin_root_str = str(plugin_dir.resolve())
    # Snapshot sys.modules keys so we can subtract the delta after load.
    modules_before = set(sys.modules.keys())

    try:
        loaded = load_plugin(candidate, api)
    finally:
        # Scrub any module keys this plugin introduced so back-to-back
        # smoke loads don't share cached sibling modules (tools.my_tool,
        # etc.). Also pop the plugin root from sys.path.
        for key in set(sys.modules.keys()) - modules_before:
            sys.modules.pop(key, None)
        # Defensive: the loader uses well-known short names for siblings.
        for short in ("provider", "adapter", "plugin", "handlers", "hooks",
                      "tools", "tools.my_tool"):
            sys.modules.pop(short, None)
        try:
            sys.path.remove(plugin_root_str)
        except ValueError:
            pass

    if loaded is None:
        raise RuntimeError(
            "loader returned None — check logs for import or register() errors"
        )


@plugin_app.command("new")
def plugin_new(
    name: str = typer.Argument(..., help="Plugin id (lowercase, hyphens allowed)."),
    kind: str = typer.Option(
        "",
        "--kind",
        "-k",
        help="Template kind: channel | provider | toolkit | mixed.",
    ),
    path: Path | None = typer.Option(
        None,
        "--path",
        "-p",
        help="Output directory (default: ~/.opencomputer/plugins/).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing directory with same name.",
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="Free-form plugin description.",
    ),
    author: str = typer.Option(
        "",
        "--author",
        "-a",
        help="Free-form author string.",
    ),
    no_smoke: bool = typer.Option(
        False,
        "--no-smoke",
        help=(
            "Skip the post-scaffold smoke check. Use when the template's "
            "register() needs external pip deps you haven't installed yet."
        ),
    ),
) -> None:
    """Scaffold a new plugin skeleton from the built-in templates.

    Example:
        opencomputer plugin new my-weather --kind provider
    """
    from opencomputer.agent.config import _home
    from opencomputer.cli_plugin_scaffold import render_plugin_template

    # Resolve --kind: interactive prompt when stdin is a tty or has input
    # waiting; error when truly non-interactive (CI/script with no input).
    resolved_kind = kind
    if not resolved_kind:
        # If stdin is explicitly a non-tty (CI, piped script with no data),
        # refuse. We also refuse if typer.prompt() raises Abort due to EOF.
        if not sys.stdin.isatty():
            # Best-effort: try to read — if input was piped (e.g. tests)
            # proceed; otherwise error out clearly.
            try:
                resolved_kind = typer.prompt(
                    f"Plugin kind ({', '.join(_VALID_KINDS)})",
                    default="mixed",
                )
            except (typer.Abort, EOFError):
                _console.print(
                    "[red]error:[/red] --kind required in non-interactive mode "
                    f"(one of: {', '.join(_VALID_KINDS)})"
                )
                raise typer.Exit(code=1) from None
        else:
            resolved_kind = typer.prompt(
                f"Plugin kind ({', '.join(_VALID_KINDS)})",
                default="mixed",
            )

    if resolved_kind not in _VALID_KINDS:
        _console.print(
            f"[red]error:[/red] invalid --kind {resolved_kind!r}; "
            f"must be one of {', '.join(_VALID_KINDS)}"
        )
        raise typer.Exit(code=1)

    # Resolve --path: profile-local plugin dir by default.
    output_dir = path if path is not None else _home() / "plugins"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        written = render_plugin_template(
            plugin_id=name,
            kind=resolved_kind,  # type: ignore[arg-type]
            output_path=output_dir,
            description=description,
            author=author,
            overwrite=force,
        )
    except FileExistsError:
        target = output_dir / name
        _console.print(
            f"[red]error:[/red] Plugin '{name}' already exists at {target}. "
            "Pass --force to overwrite."
        )
        raise typer.Exit(code=1) from None
    except ValueError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except Exception as e:  # noqa: BLE001
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None

    target = output_dir / name
    _console.print(
        f"[green]Scaffolded[/green] {name} ({resolved_kind}) at {target}/"
    )
    _console.print("")
    _console.print("[bold]Created files:[/bold]")
    for p in written:
        try:
            rel = p.relative_to(target)
        except ValueError:
            rel = p
        _console.print(f"  - {rel}")
    _console.print("")
    _console.print("[bold]Next steps:[/bold]")
    _console.print(f"  1. cd {target}")
    _console.print("  2. Open plugin.py and fill in the TODO(s).")
    _console.print("  3. Run tests:  pytest tests/")
    _console.print("  4. opencomputer plugins    # verify it loaded")

    # Post-scaffold smoke check — verify the freshly-rendered plugin
    # actually loads through the real loader with an isolated registry
    # so template regressions are caught here, not at agent startup.
    if not no_smoke:
        try:
            _smoke_load_plugin(target)
        except Exception as e:  # noqa: BLE001
            _console.print("")
            _console.print(
                f"[red]✗ Smoke check failed — plugin raised:[/red] {e}"
            )
            raise typer.Exit(code=1) from None
        _console.print("")
        _console.print(
            "[green]✓ Smoke check passed — plugin loads cleanly.[/green]"
        )


__all__ = ["plugin_app"]
