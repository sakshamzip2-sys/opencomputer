"""T5 — `oc honcho` CLI subcommand group.

Hermes-doc parity. Five subcommands:

* ``status``   — show provider / cadence / reasoning level / detected preset
* ``enable``   — set ``memory.provider = honcho`` in this profile's config
* ``disable``  — set ``memory.provider = builtin``
* ``strategy`` — apply a cadence + reasoning-level preset (low / balanced / aggressive)
* ``sync``     — backfill Honcho peers across all profiles (best-effort, idempotent)

Reads/writes ``<OPENCOMPUTER_HOME>/config.yaml`` (the active profile's
config). All writes go through a single helper that preserves any
existing keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console

from opencomputer.agent.config import _home

console = Console()
honcho_app = typer.Typer(
    name="honcho",
    help="Manage the Honcho memory provider (Hermes-doc parity).",
    no_args_is_help=True,
)


# ─── strategy presets ────────────────────────────────────────────

#: Cadence + reasoning preset for the ``strategy`` subcommand.
#: ``low`` minimises Honcho cost; ``aggressive`` maximises depth.
_PRESETS: dict[str, dict[str, Any]] = {
    "low": {
        "context_cadence": 4,
        "dialectic_cadence": 8,
        "dialectic_reasoning_level": "low",
    },
    "balanced": {
        "context_cadence": 2,
        "dialectic_cadence": 4,
        "dialectic_reasoning_level": "low",
    },
    "aggressive": {
        "context_cadence": 1,
        "dialectic_cadence": 2,
        "dialectic_reasoning_level": "medium",
    },
}


def _config_path() -> Path:
    """Return ``<OPENCOMPUTER_HOME>/config.yaml`` for the active profile."""
    return _home() / "config.yaml"


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        console.print(f"[red]Could not parse {path} — refusing to overwrite.[/red]")
        raise typer.Exit(code=1) from None


def _save_config(data: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _set_memory_provider(provider: str) -> None:
    cfg = _load_config()
    cfg.setdefault("memory", {})
    cfg["memory"]["provider"] = provider
    _save_config(cfg)


def _detect_preset(memory: dict[str, Any]) -> str:
    """Return the preset name whose values match ``memory``, or ``custom``."""
    for name, preset in _PRESETS.items():
        if all(memory.get(k) == v for k, v in preset.items()):
            return name
    return "custom"


@honcho_app.command("status")
def status() -> None:
    """Show Honcho provider state — provider / cadence / reasoning level."""
    cfg = _load_config()
    memory = cfg.get("memory", {}) or {}
    provider = memory.get("provider", "builtin")
    cadence_ctx = memory.get("context_cadence", "<unset>")
    cadence_dial = memory.get("dialectic_cadence", "<unset>")
    level = memory.get("dialectic_reasoning_level", "<unset>")
    preset = _detect_preset(memory)
    console.print("Honcho status:")
    console.print(f"  provider: [bold]{provider}[/bold]")
    console.print(f"  context_cadence: {cadence_ctx}")
    console.print(f"  dialectic_cadence: {cadence_dial}")
    console.print(f"  dialectic_reasoning_level: {level}")
    console.print(f"  preset: [bold]{preset}[/bold]")


@honcho_app.command("enable")
def enable() -> None:
    """Set ``memory.provider = honcho`` in this profile's config."""
    _set_memory_provider("honcho")
    console.print(
        "[green]Honcho enabled[/green]. Run "
        "[bold]oc honcho status[/bold] to verify."
    )


@honcho_app.command("disable")
def disable() -> None:
    """Set ``memory.provider = builtin`` in this profile's config."""
    _set_memory_provider("builtin")
    console.print(
        "[yellow]Honcho disabled[/yellow] — built-in memory active."
    )


@honcho_app.command("strategy")
def strategy(
    name: str = typer.Argument(
        ..., help="Preset: low / balanced / aggressive"
    ),
) -> None:
    """Apply a cadence + reasoning-level preset to this profile."""
    if name not in _PRESETS:
        console.print(
            f"[red]Unknown preset '{name}'.[/red] "
            f"Choose one of: {', '.join(_PRESETS.keys())}"
        )
        raise typer.Exit(code=1)
    cfg = _load_config()
    cfg.setdefault("memory", {})
    cfg["memory"].update(_PRESETS[name])
    _save_config(cfg)
    console.print(f"[green]Applied '{name}' preset[/green]:")
    for k, v in _PRESETS[name].items():
        console.print(f"  {k}: {v}")


@honcho_app.command("sync")
def sync() -> None:
    """Backfill Honcho peers across all profiles (best-effort, idempotent).

    Iterates ``~/.opencomputer/<profile>/`` and ensures the AI peer
    exists for any profile with ``memory.provider == honcho``. Silently
    skips profiles where the Honcho server is unreachable — sync is a
    convenience, not a hard guarantee.
    """
    home_root = Path.home() / ".opencomputer"
    if not home_root.exists() or not home_root.is_dir():
        console.print("[dim]No profiles found at ~/.opencomputer.[/dim]")
        return
    profiles = sorted(p.name for p in home_root.iterdir() if p.is_dir())
    if not profiles:
        console.print("[dim]No profiles found.[/dim]")
        return
    console.print(f"Found {len(profiles)} profile(s): {', '.join(profiles)}")
    synced = 0
    skipped = 0
    for prof in profiles:
        if _sync_one_profile(prof):
            synced += 1
        else:
            skipped += 1
    console.print(
        f"[green]Sync complete[/green]: {synced} synced, {skipped} skipped."
    )


def _sync_one_profile(profile: str) -> bool:
    """Best-effort: load the Honcho bootstrap and ensure the AI peer.

    Returns True on success, False on any failure. ``sync`` is best
    effort — failures here are silent.
    """
    try:
        import importlib.util
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        bootstrap_py = repo_root / "extensions" / "memory-honcho" / "bootstrap.py"
        if not bootstrap_py.exists():
            return False
        mod_name = f"_honcho_bootstrap_{profile}"
        spec = importlib.util.spec_from_file_location(mod_name, bootstrap_py)
        if spec is None or spec.loader is None:
            return False
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        ensure_peer = getattr(mod, "honcho_ensure_peer", None)
        if ensure_peer is None:
            # Older bootstrap with no ensure_peer helper — nothing to do.
            return False
        return bool(ensure_peer(profile=profile))
    except Exception:  # noqa: BLE001
        return False
