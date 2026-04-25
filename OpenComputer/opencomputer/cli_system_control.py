"""``opencomputer system-control`` Typer subapp (Phase 3.F).

Subcommands::

    opencomputer system-control enable [--menu-bar]
    opencomputer system-control disable
    opencomputer system-control status

3.F is the **administrative gate** for autonomous full-system-control
mode. Independent of F1 consent gating: F1 controls per-tool authorization;
this controls the whole autonomous-mode personality. Both must be on for
autonomous tool execution.

When 3.F is OFF (the default), nothing visible changes for users running
the agent in chat mode. When ON, the structured ``agent.log`` collector
+ optional menu-bar indicator activate.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console

from opencomputer.agent.config import FullSystemControlConfig
from opencomputer.agent.config_store import load_config, save_config

system_control_app = typer.Typer(
    name="system-control",
    help="Phase 3.F — autonomous full-system-control mode toggle.",
    no_args_is_help=True,
)
console = Console()
_log = logging.getLogger("opencomputer.cli.system_control")

# Module-level handle on the live MenuBarIndicator (if any) so disable
# can stop it. CLI processes are short-lived, so this is mainly useful
# inside long-running daemons (gateway, wire) that import the subapp.
_menu_bar_instance: object | None = None


def _set_enabled(enabled: bool, *, menu_bar_indicator: bool | None = None) -> None:
    """Persist the new ``system_control.enabled`` value (and optionally
    ``menu_bar_indicator``) to ``config.yaml``."""
    cfg = load_config()
    sc = cfg.system_control
    new_sc = FullSystemControlConfig(
        enabled=enabled,
        log_path=sc.log_path,
        menu_bar_indicator=(
            menu_bar_indicator if menu_bar_indicator is not None else sc.menu_bar_indicator
        ),
        json_log_max_size_bytes=sc.json_log_max_size_bytes,
    )
    # Substitute the new dataclass into the root Config and save.
    from dataclasses import fields as _fields

    kwargs = {f.name: getattr(cfg, f.name) for f in _fields(cfg)}
    kwargs["system_control"] = new_sc
    save_config(type(cfg)(**kwargs))


def _format_size(n: int) -> str:
    """Human-readable byte size (decimal-style)."""
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"


def _read_last_lines(path: Path, n: int) -> list[str]:
    """Return up to last ``n`` lines from ``path``. OSError-tolerant."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as e:
        _log.warning("system-control status: log read failed: %s", e)
        return []
    return [line.rstrip("\n") for line in lines[-n:]]


@system_control_app.command("enable")
def system_control_enable(
    menu_bar: bool = typer.Option(
        False,
        "--menu-bar",
        help="Also start the macOS menu-bar indicator (best-effort; needs rumps).",
    ),
) -> None:
    """Turn ON autonomous full-system-control mode.

    Writes ``system_control.enabled = true`` to ``config.yaml``,
    attaches the structured-logger bus listener so all SignalEvents
    are mirrored to ``agent.log``, and (optionally) starts a macOS
    menu-bar indicator on a daemon thread.
    """
    _set_enabled(True, menu_bar_indicator=menu_bar if menu_bar else None)
    # Lazy-import to avoid pulling system_control at every CLI startup.
    from opencomputer.system_control.bus_listener import attach_to_bus

    attach_to_bus()  # idempotent if already attached

    if menu_bar:
        from opencomputer.system_control.menu_bar import (
            MenuBarIndicator,
            is_menu_bar_supported,
        )

        if is_menu_bar_supported():
            global _menu_bar_instance
            try:
                indicator = MenuBarIndicator()
                indicator.start()
                _menu_bar_instance = indicator
                console.print("[dim]menu-bar indicator started[/dim]")
            except Exception as e:  # noqa: BLE001 — best-effort
                console.print(f"[yellow]menu bar start failed:[/yellow] {e}")
        else:
            console.print(
                "[yellow]menu bar not supported on this host[/yellow] — "
                "needs macOS + the optional 'rumps' extra "
                "(`pip install opencomputer[menubar]`)"
            )

    console.print(
        "⚡ OpenComputer system-control is ON. Autonomous tools may run "
        "without per-call confirmation. Disable with "
        "`opencomputer system-control disable`."
    )


@system_control_app.command("disable")
def system_control_disable() -> None:
    """Turn OFF autonomous full-system-control mode.

    Writes ``system_control.enabled = false`` to ``config.yaml``,
    detaches the bus listener if attached, and stops the menu-bar
    indicator if running.
    """
    _set_enabled(False)
    from opencomputer.system_control.bus_listener import detach_from_bus

    detach_from_bus()  # idempotent

    global _menu_bar_instance
    if _menu_bar_instance is not None:
        try:
            stop = getattr(_menu_bar_instance, "stop", None)
            if callable(stop):
                stop()
        except Exception as e:  # noqa: BLE001 — best-effort
            _log.warning("menu bar stop failed: %s", e)
        _menu_bar_instance = None

    console.print("OpenComputer system-control is OFF. Standard chat-agent mode.")


@system_control_app.command("status")
def system_control_status() -> None:
    """Show enabled state, log path + size, menu-bar status, last 5 entries."""
    cfg = load_config()
    sc = cfg.system_control
    console.print(
        f"[bold]enabled:[/bold] {'[green]on[/green]' if sc.enabled else '[dim]off[/dim]'}"
    )
    console.print(f"[bold]log path:[/bold] {sc.log_path}")
    log_path = Path(sc.log_path)
    if log_path.exists():
        try:
            size = log_path.stat().st_size
            console.print(f"[bold]log size:[/bold] {_format_size(size)}")
        except OSError as e:
            console.print(f"[bold]log size:[/bold] [yellow]unreadable: {e}[/yellow]")
    else:
        console.print("[bold]log size:[/bold] [dim](file does not exist yet)[/dim]")

    console.print(
        f"[bold]rotation threshold:[/bold] {_format_size(sc.json_log_max_size_bytes)}"
    )
    console.print(
        f"[bold]menu-bar requested:[/bold] "
        f"{'[green]yes[/green]' if sc.menu_bar_indicator else '[dim]no[/dim]'}"
    )

    # Last 5 entries
    lines = _read_last_lines(log_path, 5)
    if lines:
        console.print("\n[bold]last 5 entries:[/bold]")
        for line in lines:
            try:
                obj = json.loads(line)
                kind = obj.get("kind", "?")
                ts = obj.get("timestamp", 0.0)
                console.print(f"  [cyan]{kind}[/cyan] @ {ts:.3f}")
            except (TypeError, ValueError):
                console.print(f"  [yellow]<unparseable>[/yellow] {line[:80]}")
    else:
        console.print("\n[dim]no entries yet[/dim]")


__all__ = ["system_control_app"]
