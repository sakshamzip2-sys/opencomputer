"""``opencomputer tui`` CLI command — Ink+React TUI launcher.

Looks for the built TUI artifact at ``opencomputer/ui-tui/dist/entry.js``
(populated by ``scripts/build-tui.sh`` and shipped in the wheel via
``[tool.hatch.build.targets.wheel.force-include]``). Spawns Node via
``execvpe`` so signals propagate cleanly.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

tui_app = typer.Typer(
    name="tui",
    help="Run the Ink+React TUI (Hermes shell, OC backend).",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _entry_path() -> Path:
    """Resolve the TUI entry script.

    Order:
    1. Wheel-installed location: ``<opencomputer pkg>/ui-tui/dist/entry.js``
    2. Source-tree fallback: ``<repo root>/ui-tui/dist/entry.js``
    """
    import opencomputer

    pkg_dir = Path(opencomputer.__file__).parent
    candidate = pkg_dir / "ui-tui" / "dist" / "entry.js"
    if candidate.exists():
        return candidate
    # Source-tree fallback (developer running from a checkout)
    repo_root = pkg_dir.parent
    src_candidate = repo_root / "ui-tui" / "dist" / "entry.js"
    if src_candidate.exists():
        return src_candidate
    return candidate  # return the expected-but-missing path for the error message


@tui_app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    wire_url: Annotated[
        str, typer.Option(help="WebSocket URL of the OC wire server.")
    ] = "ws://127.0.0.1:18789",
    dashboard_url: Annotated[
        str, typer.Option(help="HTTP URL of the OC dashboard for non-streaming reads.")
    ] = "http://127.0.0.1:9119",
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    entry = _entry_path()
    if not entry.exists():
        typer.echo(
            f"TUI build artifact not found at {entry}.\n"
            "Build it from a source checkout:\n"
            "  cd OpenComputer && ./scripts/build-tui.sh\n"
            "Or install the latest wheel which ships the prebuilt TUI.",
            err=True,
        )
        raise typer.Exit(2)

    node = shutil.which("node.exe" if sys.platform == "win32" else "node")
    if not node:
        typer.echo(
            "node binary not found in PATH. Install Node 20+ to use `oc tui`.\n"
            "On macOS: brew install node@20\n"
            "On linux: see https://nodejs.org/en/download",
            err=True,
        )
        raise typer.Exit(2)

    env = os.environ.copy()
    env["OC_WIRE_URL"] = wire_url
    env["OC_DASHBOARD_URL"] = dashboard_url

    typer.echo(f"Launching TUI against {wire_url}…")
    os.execvpe(node, [node, str(entry)], env)
