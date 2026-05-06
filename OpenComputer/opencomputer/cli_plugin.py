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

  opencomputer plugin enable <id>        (Phase 12b5, Sub-project E.E4)
    Append <id> to the active profile's profile.yaml ``plugins.enabled``
    list. Friendly no-op if already enabled. Validates <id> against the
    currently-discovered plugins. Atomic write via tmp + os.replace.

  opencomputer plugin disable <id>       (Phase 12b5, Sub-project E.E4)
    Symmetric removal from the active profile's profile.yaml. Friendly
    no-op if the id isn't present. Atomic write.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

plugin_app = typer.Typer(
    name="plugin",
    help="Manage installed plugins (install/uninstall/where).",
    no_args_is_help=True,
)
_console = Console()

catalog_app = typer.Typer(
    name="catalog",
    help="Sign + verify the remote plugin catalog (Ed25519). D.3 T3.",
    no_args_is_help=True,
)
plugin_app.add_typer(catalog_app, name="catalog")


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


def _install_from_git(url: str, **kwargs):
    """Indirection for install_from_git — patchable in tests."""
    from opencomputer.plugins.remote_install import install_from_git

    return install_from_git(url, **kwargs)


def _install_from_url(url: str, **kwargs):
    """Indirection for install_from_url — patchable in tests."""
    from opencomputer.plugins.remote_install import install_from_url

    return install_from_url(url, **kwargs)


def _verify_plugin(*args, **kwargs):
    """Indirection for verify_plugin — patchable in tests."""
    from opencomputer.plugins.integrity import verify_plugin

    return verify_plugin(*args, **kwargs)


async def _composed_before_install_hook(ctx):
    """Fan-out to every registered BEFORE_INSTALL hook; first 'block' wins.

    No-op (returns None) when no handlers are registered, which is the
    typical case for a fresh `oc plugin install` invocation that hasn't
    loaded any plugins yet.
    """
    from opencomputer.hooks.engine import engine as _hook_engine

    return await _hook_engine.fire_blocking(ctx)


def _is_git_arg(arg: str) -> bool:
    return arg.startswith(("git+http", "git+ssh", "git+file", "git+https"))


def _is_url_arg(arg: str) -> bool:
    return arg.startswith(("http://", "https://"))


@plugin_app.command("install")
def install(
    source: str = typer.Argument(
        ...,
        help=(
            "Path to a local plugin directory, OR a slug to resolve via "
            "the remote catalog (use with --remote), OR a git+/https:// "
            "URL for direct install."
        ),
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
    remote: bool = typer.Option(
        False,
        "--remote",
        help=(
            "Treat SOURCE as a slug; resolve via the remote plugin catalog. "
            "Catalog URL comes from OC_PLUGIN_CATALOG_URL or "
            "config.yaml plugins.catalog_url."
        ),
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Bypass the 24h catalog cache (only with --remote).",
    ),
    plugin_id_hint: str | None = typer.Option(
        None,
        "--id",
        help=(
            "Plugin id to install as. Required for git/https URL installs; "
            "must match the plugin.json id in the source."
        ),
    ),
    sha256: str | None = typer.Option(
        None,
        "--sha256",
        help="Required for https:// tarball installs — pin the source bytes.",
    ),
    ref: str | None = typer.Option(
        None,
        "--ref",
        help="Pin a specific git sha/tag/branch (only with git+ source).",
    ),
) -> None:
    """Install a plugin from a local directory, remote catalog, git, or URL."""
    # Phase 1 (2026-05-06) — URL-scheme-routed install paths.
    if _is_git_arg(source):
        if plugin_id_hint is None:
            _console.print(
                "[red]error:[/red] git installs require --id <plugin-id> "
                "(must match the cloned repo's plugin.json id)"
            )
            raise typer.Exit(code=2)
        from opencomputer.plugins.remote_install import CatalogError

        dest_root = _resolve_destination_root(profile, is_global)
        dest_root.mkdir(parents=True, exist_ok=True)
        try:
            result = _install_from_git(
                source,
                dest_root=dest_root,
                plugin_id_hint=plugin_id_hint,
                ref=ref,
                force=force,
                before_install_hook=_composed_before_install_hook,
            )
        except CatalogError as e:
            _console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(code=1) from None
        _console.print(
            f"[green]installed:[/green] '{result.plugin_id}' "
            f"v{result.version} (git) → {result.install_path}"
        )
        return

    if _is_url_arg(source):
        if plugin_id_hint is None or sha256 is None:
            _console.print(
                "[red]error:[/red] https:// installs require both "
                "--id <plugin-id> and --sha256 <hex>"
            )
            raise typer.Exit(code=2)
        from opencomputer.plugins.remote_install import CatalogError

        dest_root = _resolve_destination_root(profile, is_global)
        dest_root.mkdir(parents=True, exist_ok=True)
        try:
            result = _install_from_url(
                source,
                dest_root=dest_root,
                plugin_id_hint=plugin_id_hint,
                sha256=sha256,
                force=force,
                before_install_hook=_composed_before_install_hook,
            )
        except CatalogError as e:
            _console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(code=1) from None
        _console.print(
            f"[green]installed:[/green] '{result.plugin_id}' "
            f"v{result.version} (url) → {result.install_path}"
        )
        return

    if remote:
        _install_from_remote(
            slug=source,
            profile=profile,
            is_global=is_global,
            force=force,
            refresh=refresh,
        )
        return

    src_path = Path(source).expanduser().resolve()
    if not src_path.exists() or not src_path.is_dir():
        _console.print(
            f"[red]error:[/red] {src_path} does not exist or is not a directory. "
            "Use --remote to install from the remote catalog by slug."
        )
        raise typer.Exit(code=1)

    manifest = _load_source_manifest(src_path)
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
    shutil.copytree(src_path, dest)
    _console.print(f"[green]installed:[/green] '{plugin_id}' → {dest}")


def _install_from_remote(
    *,
    slug: str,
    profile: str | None,
    is_global: bool,
    force: bool,
    refresh: bool,
) -> None:
    """D.3 T1 — install a plugin slug via the remote catalog."""
    from opencomputer.plugins.remote_install import (
        CatalogError,
        install_from_catalog,
    )

    dest_root = _resolve_destination_root(profile, is_global)

    try:
        result = install_from_catalog(
            slug,
            dest_root=dest_root,
            refresh=refresh,
            force=force,
            trusted_keys=_load_trusted_catalog_keys(),
            before_install_hook=_composed_before_install_hook,
        )
    except CatalogError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None

    _console.print(
        f"[green]installed:[/green] '{result.plugin_id}' "
        f"v{result.version} → {result.install_path}"
    )


def _load_trusted_catalog_keys() -> dict[str, bytes] | None:
    """Read ``~/.opencomputer/trusted_catalog_keys.json`` if present.

    Returns ``{fingerprint: pem_bytes}`` or None when no keys are
    configured (signature verification is then advisory).
    """
    import json

    try:
        from opencomputer.agent.config import _home
    except ImportError:  # pragma: no cover
        return None

    p = _home() / "trusted_catalog_keys.json"
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    out: dict[str, bytes] = {}
    for fp, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        pem = entry.get("public_key_pem", "")
        if isinstance(pem, str) and pem:
            out[fp] = pem.encode("utf-8")
    return out or None


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


_VALID_KINDS: tuple[str, ...] = (
    "channel",
    "provider",
    "toolkit",
    "mixed",
    "adapter-pack",
)


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


def _active_profile_yaml_path() -> tuple[Path, str]:
    """Return ``(profile_yaml_path, profile_name_for_display)``.

    Uses ``_home()`` (which honours ``OPENCOMPUTER_HOME``) for the profile
    dir, and ``read_active_profile()`` for the display name. A sticky-
    active named profile sets OPENCOMPUTER_HOME to its dir at CLI
    startup, so ``_home()`` already points at the right place by the
    time this runs.
    """
    from opencomputer.agent.config import _home
    from opencomputer.profiles import read_active_profile

    profile_dir = _home()
    display = read_active_profile() or "default"
    return profile_dir / "profile.yaml", display


from opencomputer.agent.profile_yaml import atomic_write_yaml as _atomic_write_yaml  # noqa: E402

# Re-exported so existing call sites keep working. New code should import
# directly from ``opencomputer.agent.profile_yaml``. Callers that do a
# read-modify-write cycle MUST wrap the cycle in
# :func:`opencomputer.profiles_lock.profile_yaml_lock` (PR #431) to avoid
# last-write-wins races between sibling-shell CLI invocations.


def _read_and_validate_profile_yaml(
    path: Path, *, action_label: str
) -> dict:
    """Read profile.yaml + run the strict schema validator.

    E.1 (PR closing the parse-path-divergence deferral): mutators
    (plugin enable / disable) used to do a tolerant raw read with ad-hoc
    type checks. The strict :func:`load_profile_config` reader inside
    the agent loop applied a fuller schema. This caused the two paths
    to disagree on edge cases — e.g. unknown top-level keys silently
    accepted by the CLI but rejected by the loop, or
    ``plugins.enabled: "*"`` accepted as a wildcard by the loop but
    treated as an "enabled list" by the CLI.

    This helper centralizes the read: it parses the raw YAML (so we
    can preserve unknown-but-currently-tolerated keys for round-trip
    write), then runs the SAME strict validator the loop uses. Both
    halves see exactly the same shape; both halves emit the same
    error. Round-trip preservation is intact because we keep the raw
    dict and only validate against it.

    Returns the raw parsed dict (always a dict — fresh empty dict if
    the file doesn't exist). Exits with code 1 on schema error, after
    printing a Rich-formatted message that includes ``action_label``
    so users see which mutator failed.
    """
    from opencomputer.agent.profile_config import (
        ProfileConfigError,
        validate_profile_config_dict,
    )

    if not path.exists():
        return {}

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        _console.print(
            f"[red]error:[/red] {path}: invalid YAML ({exc}). "
            f"Cannot {action_label} plugin until profile.yaml parses."
        )
        raise typer.Exit(code=1) from None

    if not isinstance(raw, dict):
        _console.print(
            f"[red]error:[/red] {path} must contain a YAML mapping at the top level."
        )
        raise typer.Exit(code=1)

    try:
        validate_profile_config_dict(raw, path=path)
    except ProfileConfigError as exc:
        _console.print(
            f"[red]error:[/red] {exc}\n"
            f"Fix profile.yaml before running [cyan]oc plugin {action_label}[/cyan]."
        )
        raise typer.Exit(code=1) from None

    return raw


def _try_clear_demand_tracker(plugin_id: str) -> None:
    """Best-effort clear of the demand tracker's rows for ``plugin_id``.

    Sub-project E.E4 design note Option A: construct a tracker directly
    from ``cfg.session.db_path`` with a no-op discover_fn. We only need
    ``clear()`` which doesn't invoke discover_fn at all, so there's no
    risk of walking the filesystem. Any failure is swallowed — this is
    a cleanup nice-to-have, not a correctness requirement.
    """
    try:
        from opencomputer.agent.config import default_config
        from opencomputer.plugins.demand_tracker import PluginDemandTracker

        cfg = default_config()
        tracker = PluginDemandTracker(
            db_path=cfg.session.db_path,
            discover_fn=lambda: [],  # no-op; clear() never calls this
        )
        tracker.clear(plugin_id)
    except Exception:  # noqa: BLE001
        # Intentional: the CLI should succeed even if the tracker DB
        # isn't available (fresh install, read-only FS, etc.).
        pass


@plugin_app.command("enable")
def plugin_enable(
    plugin_id: str = typer.Argument(..., help="Plugin id to enable for the active profile."),
) -> None:
    """Append ``<id>`` to the active profile's ``profile.yaml``.

    Validates the id against all discovered plugins (profile-local,
    global, bundled). Writes atomically. Friendly no-op if already
    enabled. Reminds the user to restart opencomputer since plugins are
    loaded at AgentLoop construction time.
    """
    from opencomputer.plugins.discovery import discover, standard_search_paths
    from opencomputer.profiles_lock import profile_yaml_lock

    candidates = discover(standard_search_paths())
    known_ids = {c.manifest.id for c in candidates}
    if plugin_id not in known_ids:
        _console.print(
            f"[red]error:[/red] unknown plugin id '{plugin_id}'. "
            "Run `opencomputer plugins` to see installed plugins."
        )
        raise typer.Exit(code=1)

    path, profile_name = _active_profile_yaml_path()

    with profile_yaml_lock(path.parent):
        raw = _read_and_validate_profile_yaml(path, action_label="enable")

        plugins_block = raw.get("plugins")
        if plugins_block is None:
            plugins_block = {"enabled": []}
            raw["plugins"] = plugins_block
        # validator already enforced that plugins is a dict + enabled is
        # list-or-"*" — here we just need to handle the "*" wildcard case
        # and seed an empty list.
        enabled = plugins_block.get("enabled")
        if enabled is None or enabled == "*":
            # Wildcard means "all plugins allowed"; promoting to an
            # explicit list to add a specific id would NARROW the
            # filter, which is surprising. Reject loudly.
            if enabled == "*":
                _console.print(
                    f"[red]error:[/red] {path} has `plugins.enabled: \"*\"` "
                    "(wildcard).\n"
                    "Adding an explicit id would narrow the filter; "
                    "remove the wildcard first if you want a curated list."
                )
                raise typer.Exit(code=1)
            enabled = []
            plugins_block["enabled"] = enabled

        if plugin_id in enabled:
            _console.print(
                f"Plugin '{plugin_id}' is already enabled for profile "
                f"'{profile_name}'. No change."
            )
            raise typer.Exit(code=0)

        enabled.append(plugin_id)

        _atomic_write_yaml(path, raw)

    _try_clear_demand_tracker(plugin_id)

    _console.print(
        f"[green]Enabled[/green] '{plugin_id}' for profile '{profile_name}'. "
        "Restart opencomputer to load it."
    )


@plugin_app.command("disable")
def plugin_disable(
    plugin_id: str = typer.Argument(..., help="Plugin id to disable for the active profile."),
) -> None:
    """Remove ``<id>`` from the active profile's ``profile.yaml``.

    Friendly no-op if the id isn't currently enabled (including when
    profile.yaml doesn't exist yet). Writes atomically on success.
    """
    from opencomputer.profiles_lock import profile_yaml_lock

    path, profile_name = _active_profile_yaml_path()

    def _already_not_enabled() -> None:
        _console.print(
            f"Plugin '{plugin_id}' is not enabled for profile "
            f"'{profile_name}'. Nothing to do."
        )

    if not path.exists():
        _already_not_enabled()
        raise typer.Exit(code=0)

    with profile_yaml_lock(path.parent):
        raw = _read_and_validate_profile_yaml(path, action_label="disable")

        plugins_block = raw.get("plugins")
        if not isinstance(plugins_block, dict):
            _already_not_enabled()
            raise typer.Exit(code=0)

        enabled = plugins_block.get("enabled")
        if not isinstance(enabled, list) or plugin_id not in enabled:
            _already_not_enabled()
            raise typer.Exit(code=0)

        enabled.remove(plugin_id)

        _atomic_write_yaml(path, raw)

    _console.print(
        f"[green]Disabled[/green] '{plugin_id}' for profile "
        f"'{profile_name}'. Restart opencomputer to unload it."
    )


@plugin_app.command("demand")
def plugin_demand(
    since_turns: Annotated[
        int | None,
        typer.Option(
            "--since-turns",
            help=(
                "Only show signals from the last N turns (per-session "
                "max_turn). Default: show everything."
            ),
        ),
    ] = None,
) -> None:
    """List demand signals recorded by the E2 tracker for the active DB.

    Empty state prints a helpful explainer; populated state prints a
    Rich table of ``(plugin, tool, session, turn, count)`` rows (same
    tool-not-found firing across multiple turns aggregates into ONE row
    with a count) plus a footer with the top-recommendation plugin.

    Option-A pattern: constructs a ``PluginDemandTracker`` directly with
    a no-op ``discover_fn`` — we only query, never insert, so
    ``discover_fn`` + ``active_profile_plugins`` don't matter here.
    """
    from opencomputer.agent.config import default_config
    from opencomputer.plugins.demand_tracker import PluginDemandTracker

    cfg = default_config()
    tracker = PluginDemandTracker(
        db_path=cfg.session.db_path,
        discover_fn=lambda: [],
        active_profile_plugins=None,
    )

    signals = tracker.signals_by_plugin(session_id=None)

    # Apply --since-turns filter per-session if requested. E2's
    # recommended_plugins() helper implements similar semantics but
    # returns per-plugin counts only; for table rendering we need the
    # row-level detail, so we filter the dict manually here.
    if since_turns is not None:
        # Per-session max turn — matches the semantics used for rendering
        # (each session's window is relative to that session's latest).
        max_turn_by_session: dict[str, int] = {}
        for rows in signals.values():
            for row in rows:
                sid = row["session_id"]
                ti = int(row["turn_index"])
                if sid not in max_turn_by_session or ti > max_turn_by_session[sid]:
                    max_turn_by_session[sid] = ti
        filtered: dict[str, list[dict]] = {}
        for plugin_id, rows in signals.items():
            kept = [
                r for r in rows
                if int(r["turn_index"])
                >= max_turn_by_session[r["session_id"]] - since_turns
            ]
            if kept:
                filtered[plugin_id] = kept
        signals = filtered

    if not signals:
        _console.print("No demand signals recorded yet.")
        _console.print("")
        _console.print(
            "Demand signals are emitted when the agent calls a tool that "
            "isn't\nenabled in the current profile. For example, in a "
            "profile without\ncoding-harness enabled, calls to "
            "Edit/MultiEdit/TodoWrite/etc. would\naccumulate here as a "
            "signal that this profile could benefit from\nthat plugin."
        )
        _console.print("")
        _console.print("Enable a plugin with: opencomputer plugin enable <plugin-id>")
        raise typer.Exit(code=0)

    # Aggregate (plugin_id, tool_name, session_id) → count + latest turn.
    # Using the latest turn per group gives the user a useful "when did
    # this last happen" hint; total count reflects every occurrence.
    aggregated: dict[tuple[str, str, str], dict] = {}
    plugin_totals: dict[str, int] = {}
    for plugin_id, rows in signals.items():
        for row in rows:
            key = (plugin_id, row["tool_name"], row["session_id"])
            entry = aggregated.get(key)
            if entry is None:
                entry = {"count": 0, "latest_turn": int(row["turn_index"])}
                aggregated[key] = entry
            entry["count"] += 1
            if int(row["turn_index"]) > entry["latest_turn"]:
                entry["latest_turn"] = int(row["turn_index"])
            plugin_totals[plugin_id] = plugin_totals.get(plugin_id, 0) + 1

    # Sort: count desc, then plugin_id asc for stable tiebreak.
    sorted_rows = sorted(
        aggregated.items(),
        key=lambda kv: (-kv[1]["count"], kv[0][0], kv[0][1], kv[0][2]),
    )

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Plugin")
    table.add_column("Tool")
    table.add_column("Session")
    table.add_column("Turn", justify="right")
    table.add_column("Signals", justify="right")

    for (plugin_id, tool_name, session_id), meta in sorted_rows:
        session_short = f"{session_id[:8]}…" if len(session_id) > 8 else session_id
        table.add_row(
            plugin_id,
            tool_name,
            session_short,
            str(meta["latest_turn"]),
            str(meta["count"]),
        )

    _console.print(table)

    # Footer: top recommendation — plugin with the highest total signal
    # count across all tools (alphabetical tiebreaker).
    top_plugin = min(
        plugin_totals.items(),
        key=lambda kv: (-kv[1], kv[0]),
    )
    top_id, top_count = top_plugin
    tools_for_top = {
        tool_name
        for (pid, tool_name, _sid) in aggregated
        if pid == top_id
    }
    _console.print("")
    _console.print(
        f"Top recommendation: enable '{top_id}' ({top_count} signals "
        f"across {len(tools_for_top)} tools)."
    )
    _console.print(f"Run: opencomputer plugin enable {top_id}")


@plugin_app.command("inspect")
def plugin_inspect(plugin_id: str) -> None:
    """Inspect a plugin's shape - compare manifest claims to actual registrations.

    Sub-project G (openclaw-parity) Task 7. Mirrors openclaw's
    ``plugins inspect`` command. Prints declared vs actual tools /
    channels / providers / hooks and any drift between them. Exit code
    1 when drift detected.
    """
    from opencomputer.plugins.inspect_shape import inspect_shape

    shape = inspect_shape(plugin_id)
    _console.print(f"Plugin: {shape.plugin_id}")
    _console.print(f"Status: {shape.classification}")
    _console.print("")
    _console.print("Declared tools (manifest):")
    for t in shape.declared_tools or ("(none)",):
        _console.print(f"  - {t}")
    _console.print("Actual tools (registered):")
    for t in shape.actual_tools or ("(none)",):
        _console.print(f"  - {t}")
    _console.print("")
    _console.print("Declared providers (manifest):")
    for p in shape.declared_providers or ("(none)",):
        _console.print(f"  - {p}")
    _console.print("Actual providers (registered):")
    for p in shape.actual_providers or ("(none)",):
        _console.print(f"  - {p}")
    _console.print("")
    _console.print("Declared channels (manifest):")
    for c in shape.declared_channels or ("(none)",):
        _console.print(f"  - {c}")
    _console.print("Actual channels (registered):")
    for c in shape.actual_channels or ("(none)",):
        _console.print(f"  - {c}")
    if shape.drift:
        _console.print("")
        _console.print("[yellow]DRIFT:[/yellow]")
        for d in shape.drift:
            _console.print(f"  - {d}")
        raise typer.Exit(code=1)


# ─── catalog sign / verify / keygen (D.3 T3) ──────────────────────────


@catalog_app.command("keygen")
def catalog_keygen(
    out_dir: Path | None = typer.Option(
        # Lazy default — eager Path.cwd() at module-import time crashes
        # the entire CLI when the shell's cwd has been removed.
        None,
        "--out",
        help="Directory to write catalog-signing.{key,pub} into. Defaults to CWD.",
    ),
    name: str = typer.Option(
        "catalog-signing",
        "--name",
        help="Filename prefix for the keypair (suffixes are .key + .pub).",
    ),
) -> None:
    """Generate a fresh Ed25519 keypair for catalog signing.

    Writes ``<name>.key`` (private, mode 0600) and ``<name>.pub`` (public).
    The fingerprint is printed for adding to ``trusted_catalog_keys.json``.
    """
    from opencomputer.plugins.catalog_signing import generate_keypair

    if out_dir is None:
        out_dir = Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    keypair = generate_keypair()
    key_path = out_dir / f"{name}.key"
    pub_path = out_dir / f"{name}.pub"
    key_path.write_bytes(keypair.private_pem)
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    pub_path.write_bytes(keypair.public_pem)
    _console.print(f"[green]private key:[/green] {key_path}")
    _console.print(f"[green]public key:[/green]  {pub_path}")
    _console.print(f"[green]fingerprint:[/green] {keypair.fingerprint}")


@catalog_app.command("sign")
def catalog_sign(
    catalog: Path = typer.Argument(
        ..., help="Path to the unsigned catalog JSON file."
    ),
    key: Path = typer.Option(..., "--key", help="Path to PEM private key."),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Output path for signed catalog. Defaults to overwriting input.",
    ),
) -> None:
    """Sign a catalog JSON with the given Ed25519 private key (in-place by default)."""
    import json as _json

    from opencomputer.plugins.catalog_signing import sign_catalog

    if not catalog.exists():
        _console.print(f"[red]error:[/red] {catalog} not found")
        raise typer.Exit(code=1)
    if not key.exists():
        _console.print(f"[red]error:[/red] {key} not found")
        raise typer.Exit(code=1)

    body = _json.loads(catalog.read_text(encoding="utf-8"))
    pem = key.read_bytes()
    try:
        signed = sign_catalog(body, pem)
    except Exception as e:  # noqa: BLE001
        _console.print(f"[red]error:[/red] signing failed: {e}")
        raise typer.Exit(code=1) from None

    out = output or catalog
    out.write_text(
        _json.dumps(signed.catalog, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _console.print(f"[green]signed:[/green] {out}")
    _console.print(f"[green]fingerprint:[/green] {signed.fingerprint}")


@catalog_app.command("verify")
def catalog_verify(
    catalog: Path = typer.Argument(..., help="Path to the catalog JSON file."),
    trusted_keys_path: Path | None = typer.Option(
        None,
        "--trusted-keys",
        help="Override path to trusted_catalog_keys.json. "
        "Defaults to ~/.opencomputer/trusted_catalog_keys.json.",
    ),
) -> None:
    """Verify a catalog's Ed25519 signature against trusted keys.

    Exit codes: 0 ok, 1 anything else (untrusted, tampered, missing).
    """
    import json as _json

    from opencomputer.plugins.catalog_signing import VerifyResult, verify_catalog

    if not catalog.exists():
        _console.print(f"[red]error:[/red] {catalog} not found")
        raise typer.Exit(code=1)

    keys_path = trusted_keys_path or (_load_trusted_keys_path())
    trusted = _read_trusted_keys(keys_path)
    if not trusted:
        _console.print(
            f"[yellow]warn:[/yellow] no trusted keys at {keys_path} — "
            "signature verification cannot proceed."
        )
        raise typer.Exit(code=1)

    body = _json.loads(catalog.read_text(encoding="utf-8"))
    result = verify_catalog(body, trusted)

    if result is VerifyResult.OK:
        _console.print(f"[green]verified:[/green] {catalog}")
        return
    _console.print(f"[red]verify failed:[/red] {result.name}")
    raise typer.Exit(code=1)


def _load_trusted_keys_path() -> Path:
    from opencomputer.agent.config import _home
    return _home() / "trusted_catalog_keys.json"


def _read_trusted_keys(path: Path) -> dict[str, bytes]:
    import json as _json

    if not path.exists():
        return {}
    try:
        raw = _json.loads(path.read_text(encoding="utf-8")) or {}
    except (_json.JSONDecodeError, OSError):
        return {}
    out: dict[str, bytes] = {}
    for fp, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        pem = entry.get("public_key_pem", "")
        if isinstance(pem, str) and pem:
            out[fp] = pem.encode("utf-8")
    return out


@plugin_app.command("verify")
def verify(
    plugin_id: str = typer.Argument(..., help="Plugin id to verify."),
    profile: str | None = typer.Option(None, "--profile"),
    is_global: bool = typer.Option(False, "--global"),
) -> None:
    """Compare an installed plugin's bytes against its source.

    Re-fetches the original source (catalog tarball or git ref or url
    tarball) and reports any drift versus the on-disk install. Exits
    non-zero on drift or unreachable source.
    """
    from opencomputer.plugins.installed_index import find_record

    dest_root = _resolve_destination_root(profile, is_global)
    rec = find_record(dest_root / ".installed_index.json", plugin_id)
    if rec is None:
        _console.print(f"[red]error:[/red] '{plugin_id}' is not installed")
        raise typer.Exit(code=2)

    # Catalog source needs a CLI-driven refetch_fn that goes through the
    # catalog → tarball URL. Other sources use the integrity module's default.
    refetch_fn = None
    if rec.source == "catalog":
        from opencomputer.plugins.remote_install import (
            download_and_verify,
            fetch_catalog,
            find_entry,
        )

        def refetch_fn(record):  # noqa: ARG001 — record arg unused (slug from rec)
            catalog = fetch_catalog(trusted_keys=_load_trusted_catalog_keys())
            entry = find_entry(catalog, record.plugin_id)
            return download_and_verify(entry)

    try:
        if refetch_fn is not None:
            report = _verify_plugin(
                plugin_id, dest_root=dest_root, refetch_fn=refetch_fn
            )
        else:
            report = _verify_plugin(plugin_id, dest_root=dest_root)
    except Exception as e:  # NotInstalledError / SourceUnreachableError
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=2) from None

    if not report.has_drift:
        _console.print(
            f"[green]ok:[/green] '{report.plugin_id}' has no drift "
            f"(source={report.source}, url={report.source_url})"
        )
        return

    _console.print(
        f"[yellow]drift detected:[/yellow] '{report.plugin_id}' "
        f"(source={report.source})"
    )
    for diff in report.differences:
        _console.print(f"  - {diff.kind}: {diff.path}")
    raise typer.Exit(code=1)


__all__ = ["plugin_app"]
