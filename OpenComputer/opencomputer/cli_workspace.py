"""``oc workspace`` — CLI surface for the Hermes Workspace integration.

Three subcommands:

* ``oc workspace`` (bare) / ``oc workspace run`` — start dashboard +
  workspace, open browser, block until Ctrl+C.
* ``oc workspace build [--force]`` — run ``pnpm install`` + ``pnpm build``.
* ``oc workspace doctor`` — print prerequisite + discovery status.

Discovery, prerequisites, build, launch, lifecycle live under
:mod:`opencomputer.workspace`. This CLI is a thin glue layer that
resolves the active profile, parses flags, and delegates.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from rich.console import Console

from opencomputer.workspace.builder import (
    BuildFailed,
    build_workspace,
    is_build_fresh,
    is_install_complete,
)
from opencomputer.workspace.discovery import (
    WorkspaceNotFoundError,
    discover_workspace_dir,
)
from opencomputer.workspace.lifecycle import (
    DashboardPortInUse,
    LifecycleConfig,
    WorkspaceLifecycle,
)
from opencomputer.workspace.prerequisites import check_prerequisites

__all__ = ["workspace_app"]

console = Console()
logger = logging.getLogger("opencomputer.cli_workspace")

workspace_app = typer.Typer(
    name="workspace",
    help=(
        "Launch Hermes Workspace as a browser-based control plane for OC. "
        "Sibling to `oc webui`; runs hermes-workspace (Node SSR) pointed at "
        "OC's OpenAI-compat backend."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_workspace_dir(workspace_dir: str | None) -> Path:
    from opencomputer.agent.config import _home as profile_home_fn

    try:
        profile_home = profile_home_fn()
    except Exception:  # noqa: BLE001
        profile_home = None
    try:
        return discover_workspace_dir(
            explicit=workspace_dir,
            profile_home=profile_home,
        )
    except (WorkspaceNotFoundError, ValueError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        console.print(
            "\nFix: the workspace ships in-repo at "
            "[bold]OpenComputer/oc-workspace/[/bold] — run from a source "
            "checkout, or point at a copy via [bold]--workspace-dir[/bold] "
            "or [bold]$OC_WORKSPACE_DIR[/bold]."
        )
        raise typer.Exit(code=1) from exc


def _resolve_profile_home() -> Path:
    from opencomputer.agent.config import _home as profile_home_fn

    return profile_home_fn()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@workspace_app.callback()
def _workspace_root(
    ctx: typer.Context,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Workspace bind address (default 127.0.0.1).",
    ),
    port: int = typer.Option(
        3000,
        "--port",
        help="Workspace HTTP port (default 3000).",
    ),
    dashboard_host: str = typer.Option(
        "127.0.0.1",
        "--dashboard-host",
        help="OC dashboard backend bind address (default 127.0.0.1).",
    ),
    dashboard_port: int = typer.Option(
        9119,
        "--dashboard-port",
        help="OC dashboard backend port (default 9119).",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Do not open the browser tab automatically.",
    ),
    skip_build: bool = typer.Option(
        False,
        "--skip-build",
        help=(
            "Skip the build-cache check. Use only when you know the dist/ "
            "tree is up to date."
        ),
    ),
    workspace_dir: str | None = typer.Option(
        None,
        "--workspace-dir",
        help=(
            "Path to a workspace checkout. Defaults to discovery: "
            "$OC_WORKSPACE_DIR → <profile>/workspace/ → "
            "~/.opencomputer/workspace/ → in-repo OpenComputer/oc-workspace/."
        ),
    ),
) -> None:
    """Default action when no subcommand is provided: ``oc workspace run``."""
    if ctx.invoked_subcommand is None:
        _run_impl(
            host=host,
            port=port,
            dashboard_host=dashboard_host,
            dashboard_port=dashboard_port,
            no_browser=no_browser,
            skip_build=skip_build,
            workspace_dir=workspace_dir,
        )


@workspace_app.command("run")
def workspace_run(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(3000, "--port"),
    dashboard_host: str = typer.Option("127.0.0.1", "--dashboard-host"),
    dashboard_port: int = typer.Option(9119, "--dashboard-port"),
    no_browser: bool = typer.Option(False, "--no-browser"),
    skip_build: bool = typer.Option(False, "--skip-build"),
    workspace_dir: str | None = typer.Option(None, "--workspace-dir"),
) -> None:
    """Launch the Hermes Workspace pointed at OC's backend."""
    _run_impl(
        host=host,
        port=port,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        no_browser=no_browser,
        skip_build=skip_build,
        workspace_dir=workspace_dir,
    )


def _run_impl(
    *,
    host: str,
    port: int,
    dashboard_host: str,
    dashboard_port: int,
    no_browser: bool,
    skip_build: bool,
    workspace_dir: str | None,
) -> None:
    # 1. Resolve workspace dir.
    ws_dir = _resolve_workspace_dir(workspace_dir)
    profile_home = _resolve_profile_home()

    # 1b. Plugin discovery — without this, the embedded dashboard's
    # `/v1/chat/completions` handler returns 500 with
    # `provider 'anthropic' is not registered. Installed: ['none']`
    # because the agent-side plugin registry is empty in this process.
    # `oc chat` calls `_discover_plugins()` from `cli.py` on startup;
    # `oc workspace` previously skipped it because the workspace was
    # meant to talk to a separately-running hermes-agent. Now that the
    # embedded OC dashboard serves chat directly we have to load the
    # provider/tool plugins here too.
    try:
        from opencomputer.cli import _discover_plugins

        loaded = _discover_plugins()
        logger.info("oc workspace: loaded %d plugin(s)", loaded)
    except Exception as exc:  # noqa: BLE001
        # Plugin discovery failure is non-fatal — the workspace UI
        # still boots (sessions, memory, files, terminal all work)
        # and chat will surface its own 'provider not registered'
        # error if the user tries to send a message.
        logger.warning("oc workspace: plugin discovery failed: %s", exc)

    # 2. Prerequisites.
    prereqs = check_prerequisites()
    if not prereqs.ok:
        console.print("[red]error:[/red] missing prerequisites:\n")
        for line in prereqs.report_lines():
            console.print(f"  {line}")
        raise typer.Exit(code=1)

    # 3. Build (cached).
    if not skip_build:
        try:
            outcome = build_workspace(
                ws_dir,
                pnpm_path=prereqs.pnpm.path,
                force=False,
            )
            console.print(f"[dim]workspace: {outcome.summary()}[/dim]")
        except BuildFailed as exc:
            console.print(
                f"[red]error:[/red] {exc}. Rerun with "
                "[bold]oc workspace build[/bold] for a clean retry."
            )
            raise typer.Exit(code=exc.returncode) from exc
        except FileNotFoundError as exc:
            console.print(f"[red]error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
    elif not is_build_fresh(ws_dir):
        console.print(
            "[yellow]warning:[/yellow] --skip-build set but dist/ is missing or stale; "
            "node will fail. Drop the flag or run `oc workspace build`."
        )
        raise typer.Exit(code=1)

    # 4. Banner — be honest about what works and what doesn't.
    console.print(
        f"[bold cyan]oc workspace[/bold cyan] → "
        f"http://{host}:{port}"
    )
    console.print(
        f"  backend:    http://{dashboard_host}:{dashboard_port} (oc dashboard)"
    )
    console.print(f"  workspace:  {ws_dir}")
    console.print(f"  profile:    {profile_home.name} ({profile_home})")
    console.print(
        "  [dim]chat → OC AgentLoop via /v1/chat/completions; Sessions / "
        "Skills / MCP tabs may show 'Not Available' (parity follow-up).[/dim]"
    )

    # 5. Launch via lifecycle.
    config = LifecycleConfig(
        workspace_dir=ws_dir,
        profile_home=profile_home,
        workspace_host=host,
        workspace_port=port,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        node_path=prereqs.node.path or "node",
        open_browser=not no_browser,
    )
    lifecycle = WorkspaceLifecycle(config)
    try:
        exit_code = lifecycle.run()
    except DashboardPortInUse as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except FileNotFoundError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        console.print("\n[dim]workspace stopped[/dim]")
        raise typer.Exit(code=130) from None
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=exit_code)


@workspace_app.command("build")
def workspace_build(
    force: bool = typer.Option(
        False,
        "--force",
        help="Rebuild even when the cache says it's fresh.",
    ),
    workspace_dir: str | None = typer.Option(None, "--workspace-dir"),
) -> None:
    """Run ``pnpm install`` + ``pnpm build`` for hermes-workspace."""
    ws_dir = _resolve_workspace_dir(workspace_dir)
    prereqs = check_prerequisites()
    if not prereqs.ok:
        console.print("[red]error:[/red] missing prerequisites:\n")
        for line in prereqs.report_lines():
            console.print(f"  {line}")
        raise typer.Exit(code=1)
    try:
        outcome = build_workspace(
            ws_dir, pnpm_path=prereqs.pnpm.path, force=force,
        )
    except BuildFailed as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.returncode) from exc
    except FileNotFoundError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✓[/green] {outcome.summary()}")


@workspace_app.command("doctor")
def workspace_doctor(
    workspace_dir: str | None = typer.Option(None, "--workspace-dir"),
) -> None:
    """Print prerequisite status, discovered workspace dir, build state."""
    console.print("[bold]oc workspace doctor[/bold]\n")

    # 1. Prerequisites.
    prereqs = check_prerequisites()
    for line in prereqs.report_lines():
        console.print(f"  {line}")

    # 2. Workspace discovery.
    console.print()
    profile_home = _resolve_profile_home()
    try:
        ws_dir = discover_workspace_dir(
            explicit=workspace_dir,
            profile_home=profile_home,
        )
        console.print(f"  workspace dir: [green]OK[/green] — {ws_dir}")
    except (WorkspaceNotFoundError, ValueError) as exc:
        console.print(f"  workspace dir: [red]MISSING[/red] — {exc}")
        raise typer.Exit(code=1) from exc

    # 3. Build state.
    install_ok = is_install_complete(ws_dir)
    build_ok = is_build_fresh(ws_dir)
    console.print(
        f"  node_modules:  "
        f"{'[green]OK[/green]' if install_ok else '[yellow]NOT INSTALLED[/yellow] — run `oc workspace build`'}"
    )
    console.print(
        f"  dist/server:   "
        f"{'[green]FRESH[/green]' if build_ok else '[yellow]STALE OR MISSING[/yellow] — run `oc workspace build`'}"
    )

    # 4. Env hints.
    console.print()
    console.print(
        "  [dim]env:[/dim] "
        f"OC_WORKSPACE_DIR={os.environ.get('OC_WORKSPACE_DIR') or '(unset)'} "
        f"OC_WORKSPACE_PORT={os.environ.get('OC_WORKSPACE_PORT') or '(unset)'}"
    )

    ok = prereqs.ok and install_ok and build_ok
    if not ok:
        raise typer.Exit(code=1)
