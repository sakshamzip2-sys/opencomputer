"""``oc skills`` Skills Hub CLI surface (Tier 1.A).

Per ``docs/superpowers/plans/2026-04-28-hermes-tier1a-DECISIONS.md`` D-0.6,
this module ATTACHES new hub commands onto the EXISTING ``skills_app``
in ``cli_skills.py`` (which hosts the evolution review surface
``list/review/accept/reject/evolution``). New hub commands:

- ``oc skills search [query]`` — multi-source search
- ``oc skills browse`` — list-all alias for search ""
- ``oc skills inspect <id>`` — rich metadata view
- ``oc skills install <id>`` — fetch + scan + install
- ``oc skills uninstall <id>`` — remove an installed hub skill
- ``oc skills installed`` — list hub-installed skills (NOT ``list``,
  which is reserved for evolution proposals)
- ``oc skills audit`` — view install/uninstall audit log
- ``oc skills update <id>`` — uninstall + install (idempotent re-fetch)

All command logic lives in shared ``do_*`` functions so the slash
command bridge (Phase 4 follow-up) can call them too.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

import opencomputer.skills_guard as skills_guard_module
from opencomputer.skills_hub.audit_log import AuditLog
from opencomputer.skills_hub.installer import Installer, InstallError
from opencomputer.skills_hub.lockfile import HubLockFile
from opencomputer.skills_hub.router import SkillSourceRouter
from opencomputer.skills_hub.sources.github import GitHubSource
from opencomputer.skills_hub.sources.well_known import WellKnownSource
from opencomputer.skills_hub.taps import TapsManager

console = Console()


def _profile_home() -> Path:
    """Resolve the active profile's directory.

    Test override: ``OPENCOMPUTER_HOME`` env var routes to ``<env>/default``
    so unit tests can isolate state without touching the real profile system.
    """
    env_home = os.environ.get("OPENCOMPUTER_HOME")
    if env_home:
        return Path(env_home) / "default"
    # Lazy import to avoid circular dep when this module is imported as part
    # of cli_skills.py composition.
    from opencomputer.profiles import get_profile_dir, read_active_profile

    return get_profile_dir(read_active_profile())


def _hub_root() -> Path:
    return _profile_home() / "skills" / ".hub"


def _build_router() -> SkillSourceRouter:
    from opencomputer.skills_hub.sources.minimax import MiniMaxSource

    clone_root = _hub_root() / "_clones"
    # Wave 6.E.2 — MiniMax is a default tap so users can
    # `oc skills install minimax/<skill>` with no setup. Sits next to
    # WellKnownSource as a curated, stable-identifier source.
    sources: list = [WellKnownSource(), MiniMaxSource(clone_root=clone_root)]
    taps_path = _hub_root() / "taps.json"
    for repo in TapsManager(taps_path).list():
        sources.append(GitHubSource(repo=repo, clone_root=clone_root))
    return SkillSourceRouter(sources)


def _build_installer() -> Installer:
    return Installer(
        router=_build_router(),
        skills_guard=skills_guard_module,
        hub_root=_hub_root(),
    )


# --- Shared do_* functions ---


def do_search(query: str, source: str | None = None, limit: int = 10) -> None:
    router = _build_router()
    if source and source not in router.list_sources():
        console.print(
            f"[red]Unknown source {source!r}. "
            f"Known sources: {', '.join(router.list_sources())}[/]"
        )
        return
    results = router.search(query, limit=limit, source_filter=source)
    if not results:
        console.print(f"[yellow]No matches for {query!r}[/]")
        return
    table = Table()
    table.add_column("Source", style="dim")
    table.add_column("Identifier", style="cyan")
    table.add_column("Description", overflow="fold")
    for r in results:
        table.add_row(r.source, r.identifier, r.description)
    console.print(table)


def do_inspect(identifier: str) -> bool:
    router = _build_router()
    meta = router.inspect(identifier)
    if meta is None:
        console.print(f"[red]Not found: {identifier}[/]")
        return False
    console.print(f"[bold]{meta.identifier}[/]")
    console.print(f"  description: {meta.description}")
    if meta.version:
        console.print(f"  version: {meta.version}")
    if meta.author:
        console.print(f"  author: {meta.author}")
    if meta.tags:
        console.print(f"  tags: {', '.join(meta.tags)}")
    console.print(f"  trust_level: {meta.trust_level}")
    return True


def do_install(identifier: str, yes: bool = False, force: bool = False) -> bool:
    if not yes:
        console.print(
            f"Install [bold]{identifier}[/]? Skills Guard scan will run."
        )
        confirm = typer.confirm("Proceed?", default=True)
        if not confirm:
            console.print("Aborted.")
            return False
    try:
        installer = _build_installer()
        result = installer.install(identifier, force=force)
    except InstallError as e:
        console.print(f"[red]Install failed:[/] {e}")
        return False
    console.print(f"[green]Installed[/] {identifier} → {result.install_path}")
    return True


def do_uninstall(identifier: str, yes: bool = False) -> bool:
    if not yes:
        confirm = typer.confirm(f"Uninstall {identifier}?", default=True)
        if not confirm:
            console.print("Aborted.")
            return False
    try:
        installer = _build_installer()
        installer.uninstall(identifier)
    except InstallError as e:
        console.print(f"[red]Uninstall failed:[/] {e}")
        return False
    console.print(f"[green]Uninstalled[/] {identifier}")
    return True


def do_installed() -> None:
    lockfile = HubLockFile(_hub_root() / "lockfile.json")
    entries = lockfile.list()
    if not entries:
        console.print("[dim]No hub-installed skills.[/]")
        return
    table = Table()
    table.add_column("Identifier", style="cyan")
    table.add_column("Version")
    table.add_column("Source", style="dim")
    table.add_column("Installed At", style="dim")
    for e in entries:
        table.add_row(e.identifier, e.version, e.source, e.installed_at)
    console.print(table)


def do_audit(action: str | None = None) -> None:
    log = AuditLog(_hub_root() / "audit.log")
    entries = log.entries(action=action)
    if not entries:
        console.print("[dim]Audit log is empty.[/]")
        return
    for e in entries:
        ts = e.get("timestamp", "?")
        act = e.get("action", "?")
        ident = e.get("identifier", "?")
        verdict = e.get("verdict", "")
        suffix = f" verdict={verdict}" if verdict else ""
        # Escape brackets so Rich doesn't interpret action as a markup style
        console.print(f"  {ts}  \\[{act}]  {ident}{suffix}")


def do_update(identifier: str, yes: bool = False) -> bool:
    """Update = uninstall + reinstall. Future: atomic via staging."""
    if not yes:
        confirm = typer.confirm(f"Update {identifier}?", default=True)
        if not confirm:
            return False
    do_uninstall(identifier, yes=True)
    return do_install(identifier, yes=True)


# --- attach_hub_commands: plumb commands into the existing skills_app ---


def attach_hub_commands(app: typer.Typer) -> None:
    """Add hub commands to the existing oc skills Typer app.

    Called from cli_skills.py after the existing ``app`` (with evolution
    commands list/review/accept/reject/evolution) is built. This avoids
    a second ``add_typer(name="skills")`` registration which would be a
    name collision in cli.py.
    """

    @app.command("search")
    def cmd_search(
        query: str = typer.Argument("", help="Search term (empty = list all)"),
        source: str | None = typer.Option(
            None, "--source", help="Filter to one source (e.g. 'well-known')"
        ),
        limit: int = typer.Option(10, "--limit"),
    ) -> None:
        do_search(query, source=source, limit=limit)

    @app.command("browse")
    def cmd_browse(
        source: str | None = typer.Option(None, "--source"),
        limit: int = typer.Option(20, "--limit"),
    ) -> None:
        """Browse all skills (alias for ``search`` with empty query)."""
        do_search("", source=source, limit=limit)

    @app.command("inspect")
    def cmd_inspect(identifier: str) -> None:
        ok = do_inspect(identifier)
        if not ok:
            raise typer.Exit(code=1)

    @app.command("install")
    def cmd_install(
        identifier: str,
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
        force: bool = typer.Option(
            False, "--force", help="Bypass Skills Guard ask-decisions"
        ),
    ) -> None:
        ok = do_install(identifier, yes=yes, force=force)
        if not ok:
            raise typer.Exit(code=1)

    @app.command("uninstall")
    def cmd_uninstall(
        identifier: str,
        yes: bool = typer.Option(False, "--yes", "-y"),
    ) -> None:
        ok = do_uninstall(identifier, yes=yes)
        if not ok:
            raise typer.Exit(code=1)

    @app.command("installed")
    def cmd_installed() -> None:
        """List hub-installed skills (vs ``list`` which lists evolution proposals)."""
        do_installed()

    @app.command("audit")
    def cmd_audit(
        action: str | None = typer.Option(
            None,
            "--action",
            help="Filter to install/uninstall/update/scan_blocked",
        ),
    ) -> None:
        do_audit(action=action)

    @app.command("update")
    def cmd_update(
        identifier: str,
        yes: bool = typer.Option(False, "--yes", "-y"),
    ) -> None:
        do_update(identifier, yes=yes)

    # Tap subgroup — manage GitHub repos as additional skill sources.
    tap_app = typer.Typer(
        name="tap",
        help="Manage GitHub repo taps for the skills hub.",
        no_args_is_help=True,
    )
    app.add_typer(tap_app, name="tap")

    @tap_app.command("add")
    def cmd_tap_add(repo: str) -> None:
        mgr = TapsManager(_hub_root() / "taps.json")
        try:
            mgr.add(repo)
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(code=1) from e
        console.print(f"[green]Tapped[/] {repo}")

    @tap_app.command("remove")
    def cmd_tap_remove(repo: str) -> None:
        mgr = TapsManager(_hub_root() / "taps.json")
        try:
            mgr.remove(repo)
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(code=1) from e
        console.print(f"[green]Untapped[/] {repo}")

    @tap_app.command("list")
    def cmd_tap_list() -> None:
        mgr = TapsManager(_hub_root() / "taps.json")
        taps = mgr.list()
        if not taps:
            console.print("[dim]No taps registered.[/]")
            return
        for t in taps:
            console.print(f"  {t}")
