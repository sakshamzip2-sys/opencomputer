"""Phase 14.B — ``opencomputer profile`` CLI subcommand group.

Gives the user direct management of profiles (the multi-profile state
introduced in Phase 14.A). Each profile is an independent directory
under ``~/.opencomputer/profiles/<name>/`` holding its own
``MEMORY.md``, ``USER.md``, ``config.yaml``, ``skills/``, etc.

Subcommands:

    opencomputer profile                      — show active profile status
    opencomputer profile list                 — table of all profiles
    opencomputer profile create <name>        — create a new profile
            [--clone-from X] [--clone-all]
    opencomputer profile use <name>           — set sticky active profile
    opencomputer profile delete <name> [--yes]
    opencomputer profile rename <old> <new>   — move dir + update sticky
    opencomputer profile path [<name>]        — filesystem path lookup
"""

from __future__ import annotations

import socket
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.profile_bootstrap.bridge_state import load_or_create
from opencomputer.profile_bootstrap.deepening import run_deepening  # noqa: F401
from opencomputer.profile_bootstrap.orchestrator import run_bootstrap
from opencomputer.profiles import (
    ProfileExistsError,
    ProfileNameError,
    ProfileNotFoundError,
    create_profile,
    delete_profile,
    get_default_root,
    get_profile_dir,
    list_profiles,
    read_active_profile,
    rename_profile,
    write_active_profile,
)

profile_app = typer.Typer(
    name="profile",
    help="Manage OpenComputer profiles (create/list/use/delete/rename/path).",
    invoke_without_command=True,
)
_console = Console()

# ─── bootstrap (Layered Awareness MVP install-time flow) ─────────────────


@profile_app.command("bootstrap")
def profile_bootstrap(
    skip_interview: bool = typer.Option(
        False, "--skip-interview", help="Skip the 5-question quick interview"
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run even if already completed"
    ),
    days: int = typer.Option(
        7, "--days", help="Look-back window for Layer 2 file/git scan"
    ),
) -> None:
    """Run the install-time bootstrap (Layered Awareness MVP, Layers 0-2).

    Reads system identity, asks 5 quick questions, scans the last 7 days
    of recent files + git activity. Total time: under 6 minutes.
    """
    from pathlib import Path

    from opencomputer.agent.config import _home
    from opencomputer.profile_bootstrap.identity_reflex import gather_identity
    from opencomputer.profile_bootstrap.quick_interview import (
        QUICK_INTERVIEW_QUESTIONS,
        render_questions,
    )

    home = _home()
    marker = home / "profile_bootstrap" / "complete.json"
    if marker.exists() and not force:
        typer.echo("Bootstrap already complete. Use --force to re-run.")
        raise typer.Exit(0)

    facts = gather_identity()

    answers: dict[str, str] = {}
    if not skip_interview:
        rendered = render_questions(facts)
        typer.echo(rendered[0])  # greeting
        for (key, _), prompt in zip(QUICK_INTERVIEW_QUESTIONS, rendered[1:], strict=True):
            answer = typer.prompt(prompt, default="", show_default=False)
            if answer.strip():
                answers[key] = answer.strip()

    home_dirs = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "Downloads",
    ]
    git_repos = _detect_git_repos()

    result = run_bootstrap(
        interview_answers=answers,
        scan_roots=[d for d in home_dirs if d.exists()],
        git_repos=git_repos,
        include_calendar=True,
        include_browser_history=True,
        marker_path=marker,
    )

    typer.echo("")
    typer.echo("Bootstrap complete:")
    typer.echo(f"  Identity nodes written:    {result.identity_nodes_written}")
    typer.echo(f"  Interview nodes written:   {result.interview_nodes_written}")
    typer.echo(f"  Files scanned:             {result.files_scanned}")
    typer.echo(f"  Files → graph nodes:       {result.recent_file_nodes_written}")
    typer.echo(f"  Git commits scanned:       {result.git_commits_scanned}")
    typer.echo(f"  Git → graph nodes:         {result.git_nodes_written}")
    typer.echo(f"  Calendar events scanned:   {result.calendar_events_scanned}")
    typer.echo(f"  Calendar → graph nodes:    {result.calendar_nodes_written}")
    typer.echo(f"  Browser visits scanned:    {result.browser_visits_scanned}")
    typer.echo(f"  Browser → graph nodes:     {result.browser_nodes_written}")
    typer.echo(f"  Elapsed:                   {result.elapsed_seconds:.1f}s")


@profile_app.command("deepen")
def profile_deepen(
    force: bool = typer.Option(
        False, "--force", help="Bypass idle check; run regardless of CPU/battery"
    ),
    max_artifacts: int = typer.Option(
        500, "--max-artifacts", help="Cap artifacts processed in this window"
    ),
) -> None:
    """Run one deepening pass (Layer 3 of Layered Awareness).

    Walks the current window from the cursor, extracts motifs via Ollama,
    and advances to the next window. With --force, ignores idle gating.
    """
    from pathlib import Path

    home_dirs = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "Downloads",
    ]
    git_repos = _detect_git_repos()  # already exists from V1

    result = run_deepening(
        scan_roots=[d for d in home_dirs if d.exists()],
        git_repos=git_repos,
        max_artifacts_per_window=max_artifacts,
        force=force,
    )

    if result.skipped_reason:
        typer.echo(f"Deepening skipped: {result.skipped_reason}")
        typer.echo("Use --force to run anyway.")
        return

    typer.echo("Deepening pass complete:")
    typer.echo(f"  Window processed (days):    {result.window_processed_days}")
    typer.echo(f"  Artifacts processed:        {result.artifacts_processed}")
    typer.echo(f"  Motifs emitted:             {result.motifs_emitted}")
    typer.echo(f"  Elapsed:                    {result.elapsed_seconds:.1f}s")


def _detect_git_repos(max_repos: int = 50) -> list:
    """Find candidate git repos in common locations. Best-effort, capped."""
    from pathlib import Path

    candidates = [
        Path.home() / "Vscode",
        Path.home() / "Projects",
        Path.home() / "Code",
        Path.home() / "src",
    ]
    repos = []
    for root in candidates:
        if not root.exists():
            continue
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if (entry / ".git").exists():
                repos.append(entry)
                if len(repos) >= max_repos:
                    return repos
    return repos


# ─── Bridge subapp ────────────────────────────────────────────────────────

bridge_app = typer.Typer(
    help="Browser-bridge controls (Layer 4 of Layered Awareness)",
)
profile_app.add_typer(bridge_app, name="bridge")


@bridge_app.command("token")
def bridge_token(
    rotate: bool = typer.Option(
        False, "--rotate", help="Generate a fresh token (invalidates old)"
    ),
) -> None:
    """Print the bridge auth token. Generates one on first call."""
    state = load_or_create(rotate=rotate)
    typer.echo(
        "Paste this into the browser extension's DevTools console:\n"
        f"  chrome.storage.local.set({{ ocBridgeToken: '{state.token}' }})\n"
    )
    typer.echo(state.token)


@bridge_app.command("status")
def bridge_status() -> None:
    """Show bridge config + whether port is reachable."""
    state = load_or_create()
    typer.echo(f"Token configured: {'yes' if state.token else 'no'}")
    typer.echo(f"Bind port: {state.port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(("127.0.0.1", state.port))
        typer.echo("Listener: REACHABLE")
    except (OSError, TimeoutError):
        typer.echo("Listener: NOT REACHABLE (run 'opencomputer profile bridge start')")
    finally:
        sock.close()


def _load_browser_bridge_adapter() -> type:
    """Resolve ``BrowserBridgeAdapter`` across plugin-loader / package modes.

    The hyphenated directory ``extensions/browser-bridge/`` is not a
    real Python package, so we can't rely on a single import path:

    * Tests register an ``extensions.browser_bridge`` alias via the
      conftest fixture and import as a package.
    * Production users invoke the CLI without that alias — fall back
      to ``importlib`` against the ``adapter.py`` file directly so the
      command works whether or not the plugin loader has run.
    """
    try:
        from extensions.browser_bridge.adapter import BrowserBridgeAdapter

        return BrowserBridgeAdapter
    except ImportError:
        import importlib.util

        adapter_path = (
            Path(__file__).resolve().parent.parent
            / "extensions"
            / "browser-bridge"
            / "adapter.py"
        )
        spec = importlib.util.spec_from_file_location(
            "browser_bridge_adapter", str(adapter_path)
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"browser-bridge adapter not found at {adapter_path}; "
                "is the install corrupted?"
            ) from None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.BrowserBridgeAdapter


@bridge_app.command("start")
def bridge_start(
    bind: str = typer.Option(
        "127.0.0.1", "--bind", help="Address to bind (default 127.0.0.1)"
    ),
) -> None:
    """Start the browser-bridge HTTP listener (foreground; Ctrl-C to stop).

    Uses the token + port from ``<profile_home>/profile_bootstrap/bridge.json``
    (run ``opencomputer profile bridge token`` first to seed it). The
    listener publishes ``browser_visit`` events to the module-level
    :data:`opencomputer.ingestion.bus.default_bus`, which is the shared
    singleton subscribed to by every in-process consumer (B3 trajectory,
    F2 inference, etc.). A fresh bus would be silently isolated, so we
    must use the singleton.
    """
    import asyncio

    from opencomputer.ingestion.bus import get_default_bus

    state = load_or_create()
    if not state.token:
        typer.echo(
            "No token configured. Run 'opencomputer profile bridge token' first.",
            err=True,
        )
        raise typer.Exit(1)

    BrowserBridgeAdapter = _load_browser_bridge_adapter()
    bus = get_default_bus()
    adapter = BrowserBridgeAdapter(
        bus=bus, port=state.port, token=state.token, bind=bind
    )

    async def _run() -> None:
        try:
            await adapter.start()
        except OSError as e:
            typer.echo(
                f"Failed to bind {bind}:{state.port} — {e}.\n"
                f"  hint: lsof -ti:{state.port} | xargs kill -9",
                err=True,
            )
            raise typer.Exit(1) from None
        typer.echo(
            f"Browser-bridge listening on http://{bind}:{state.port}\n"
            "Press Ctrl-C to stop."
        )
        try:
            await asyncio.Event().wait()  # block forever until Ctrl-C
        finally:
            await adapter.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nShutdown.")


@bridge_app.command("stop")
def bridge_stop() -> None:
    """Stop a running browser-bridge listener bound to the configured port.

    Foreground ``bridge start`` exits on Ctrl-C; this command is for the
    case where the listener was started under a supervisor (launchd /
    systemd / nohup). It connects to the listener's port and forces a
    clean shutdown via OS signal — ``lsof`` is the simplest portable
    discovery path.
    """
    import os
    import shutil
    import subprocess

    state = load_or_create()
    lsof = shutil.which("lsof")
    if lsof is None:
        typer.echo(
            "lsof not found on PATH; cannot locate the bridge process. "
            "Stop it manually (Ctrl-C in the foreground terminal, or your "
            "supervisor's stop command).",
            err=True,
        )
        raise typer.Exit(1)
    try:
        out = subprocess.check_output(
            [lsof, "-ti", f":{state.port}"], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        typer.echo(
            f"No process listening on port {state.port}; nothing to stop."
        )
        return
    pids = [p.strip() for p in out.splitlines() if p.strip()]
    if not pids:
        typer.echo(
            f"No process listening on port {state.port}; nothing to stop."
        )
        return
    import signal

    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
            typer.echo(f"Sent SIGTERM to pid {pid}.")
        except (ProcessLookupError, PermissionError, ValueError) as e:
            typer.echo(f"Could not signal pid {pid}: {e}", err=True)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _bytes_of(path: Path) -> int:
    """Return file size in bytes, or 0 if missing / unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _skill_count(profile_dir: Path) -> int:
    skills = profile_dir / "skills"
    if not skills.is_dir():
        return 0
    return sum(1 for p in skills.iterdir() if p.is_dir() or p.suffix == ".md")


def _profile_summary(name: str | None, path: Path) -> dict[str, object]:
    """Collect summary fields for the status / list views."""
    mem_bytes = _bytes_of(path / "MEMORY.md")
    user_bytes = _bytes_of(path / "USER.md")
    return {
        "name": name or "default",
        "path": str(path),
        "exists": path.is_dir(),
        "memory_bytes": mem_bytes,
        "user_bytes": user_bytes,
        "skills": _skill_count(path),
    }


# ─── Default (no subcommand) — status view ────────────────────────────────


@profile_app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """Show the active profile and a short summary when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    active = read_active_profile()
    path = get_profile_dir(active)
    s = _profile_summary(active, path)
    name = s["name"]
    _console.print(f"[bold cyan]active profile:[/bold cyan] {name}")
    _console.print(f"[dim]path:[/dim] {s['path']}")
    if not s["exists"]:
        _console.print(
            "[yellow]note:[/yellow] directory does not yet exist (first write will create it)."
        )
    _console.print(
        f"[dim]MEMORY.md:[/dim] {s['memory_bytes']} bytes    "
        f"[dim]USER.md:[/dim] {s['user_bytes']} bytes    "
        f"[dim]skills:[/dim] {s['skills']}"
    )


# ─── list ────────────────────────────────────────────────────────────────


@profile_app.command("list")
def list_cmd() -> None:
    """List all profiles with a marker on the active one."""
    active = read_active_profile()
    names = list_profiles()
    # Always include the synthetic "default" row (the root profile).
    rows: list[tuple[str, bool, Path]] = []
    rows.append(("default", active is None, get_default_root()))
    for n in names:
        rows.append((n, n == active, get_profile_dir(n)))

    table = Table(title="profiles", show_lines=False)
    table.add_column("", justify="center", no_wrap=True)
    table.add_column("name", no_wrap=True)
    table.add_column("path", overflow="fold")
    table.add_column("MEMORY.md", justify="right")
    table.add_column("USER.md", justify="right")
    table.add_column("skills", justify="right")

    for name, is_active, path in rows:
        marker = "◆" if is_active else ""
        mem = _bytes_of(path / "MEMORY.md")
        usr = _bytes_of(path / "USER.md")
        skills = _skill_count(path)
        table.add_row(
            marker,
            name + (" (active)" if is_active else ""),
            str(path),
            str(mem),
            str(usr),
            str(skills),
        )
    _console.print(table)


# ─── create ──────────────────────────────────────────────────────────────


@profile_app.command("create")
def create_cmd(
    name: str = typer.Argument(..., help="New profile name (kebab_case / snake_case)."),
    clone_from: str | None = typer.Option(
        None,
        "--clone-from",
        help="Clone config (and profile.yaml) from this existing profile.",
    ),
    clone_all: bool = typer.Option(
        False,
        "--clone-all",
        help="With --clone-from: recursively copy the source directory.",
    ),
) -> None:
    """Create a new profile.

    Without ``--clone-from``, the directory is empty. With ``--clone-from``,
    only ``config.yaml`` (and ``profile.yaml`` if present) are copied.
    Add ``--clone-all`` to copy everything.
    """
    try:
        path = create_profile(name, clone_from=clone_from, clone_all=clone_all)
    except ProfileNameError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except ProfileExistsError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except ProfileNotFoundError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except OSError as e:
        _console.print(f"[red]error:[/red] could not create profile: {e}")
        raise typer.Exit(code=1) from None

    suffix = ""
    if clone_from and clone_all:
        suffix = f" (fully cloned from {clone_from!r})"
    elif clone_from:
        suffix = f" (config cloned from {clone_from!r})"
    _console.print(f"[green]created[/green] profile [bold]{name}[/bold] at {path}{suffix}")
    _console.print(f"[dim]tip: switch to it with `opencomputer profile use {name}`.[/dim]")


# ─── use ─────────────────────────────────────────────────────────────────


@profile_app.command("use")
def use_cmd(
    name: str = typer.Argument(..., help="Profile to make sticky-active ('default' clears)."),
) -> None:
    """Set the sticky active profile.

    Writes ``~/.opencomputer/active_profile``. Passing ``default`` removes
    the sticky file and reverts to the root profile.
    """
    if name == "default":
        write_active_profile(None)
        _console.print("[green]active profile cleared[/green] (using default / root).")
        return

    try:
        # Validate the name first so we give a clean error for bad input.
        target = get_profile_dir(name)
    except ProfileNameError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None

    if not target.is_dir():
        _console.print(
            f"[red]error:[/red] profile {name!r} does not exist at {target}. "
            f"Create it first with `opencomputer profile create {name}`."
        )
        raise typer.Exit(code=1)

    try:
        write_active_profile(name)
    except ProfileNameError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except OSError as e:
        _console.print(f"[red]error:[/red] could not write active_profile: {e}")
        raise typer.Exit(code=1) from None

    _console.print(f"[green]active profile set to[/green] [bold]{name}[/bold] ({target})")
    _console.print("[dim]takes effect on next `opencomputer` session — current session is unchanged.[/dim]")


# ─── delete ──────────────────────────────────────────────────────────────


@profile_app.command("delete")
def delete_cmd(
    name: str = typer.Argument(..., help="Profile to remove."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the interactive confirmation."),
) -> None:
    """Remove a profile directory.

    The ``default`` profile is reserved and cannot be deleted. If the
    deleted profile was the sticky active one, the sticky file is cleared.
    """
    if name == "default":
        _console.print("[red]error:[/red] the 'default' profile cannot be deleted.")
        raise typer.Exit(code=1)

    try:
        target = get_profile_dir(name)
    except ProfileNameError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None

    if not target.is_dir():
        _console.print(f"[red]error:[/red] profile {name!r} not found at {target}.")
        raise typer.Exit(code=1)

    if not yes:
        confirm = typer.confirm(
            f"delete profile {name!r} at {target}? (this is irreversible)",
            default=False,
        )
        if not confirm:
            _console.print("aborted")
            raise typer.Exit(code=1)

    try:
        delete_profile(name)
    except ProfileNotFoundError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except ProfileNameError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except OSError as e:
        _console.print(f"[red]error:[/red] could not delete profile: {e}")
        raise typer.Exit(code=1) from None

    _console.print(f"[green]deleted[/green] profile [bold]{name}[/bold] ({target})")


# ─── rename ──────────────────────────────────────────────────────────────


@profile_app.command("rename")
def rename_cmd(
    old: str = typer.Argument(..., help="Existing profile name."),
    new: str = typer.Argument(..., help="New profile name."),
) -> None:
    """Move a profile directory.

    Prints a loud warning about Honcho / memory continuity loss before
    performing the move. If the renamed profile was the sticky active
    one, the sticky file is updated to the new name.
    """
    if old == "default" or new == "default":
        _console.print("[red]error:[/red] the 'default' profile cannot be renamed.")
        raise typer.Exit(code=1)

    _console.print(
        "[yellow]Warning:[/yellow] renaming breaks Honcho continuity "
        "(AI peer model / memory association for the old name will reset "
        "for the new profile)."
    )

    try:
        new_path = rename_profile(old, new)
    except ProfileNameError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except ProfileNotFoundError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except ProfileExistsError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    except OSError as e:
        _console.print(f"[red]error:[/red] could not rename profile: {e}")
        raise typer.Exit(code=1) from None

    _console.print(f"[green]renamed[/green] [bold]{old}[/bold] -> [bold]{new}[/bold] ({new_path})")


# ─── path ────────────────────────────────────────────────────────────────


@profile_app.command("path")
def path_cmd(
    name: str | None = typer.Argument(
        None,
        help="Profile name. Omit to print the path of the active profile.",
    ),
) -> None:
    """Print the filesystem path of a profile.

    With no argument, prints the active profile's directory (the root
    for ``default``, or ``.../profiles/<name>/`` for a named profile).
    """
    if name is None:
        active = read_active_profile()
        path = get_profile_dir(active)
        typer.echo(str(path))
        return

    try:
        path = get_profile_dir(name)
    except ProfileNameError as e:
        _console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1) from None
    typer.echo(str(path))


__all__ = ["profile_app"]
