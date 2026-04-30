"""``oc model`` — interactive model + provider picker.

Hermes-parity Tier S (2026-04-30). Hermes ships ``hermes model`` as a
standalone interactive picker, separate from in-session ``/model``.
OpenComputer had ``oc models add PROVIDER MODEL`` (positional args
only — no picker) before this. This module adds a top-level
``oc model`` that walks the user through provider selection then
model selection then writes the choice to ``config.yaml``.
"""
from __future__ import annotations

from collections import defaultdict

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config_store import (
    load_config,
    save_config,
    set_value,
)
from opencomputer.agent.model_metadata import list_models

console = Console()


def _grouped_models() -> dict[str, list[str]]:
    """Return ``{provider_id: [model_id, ...]}`` from the in-memory registry."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in list_models():
        if not entry.provider_id or not entry.model_id:
            continue
        grouped[entry.provider_id].append(entry.model_id)
    # Stable ordering for picker UX.
    return {p: sorted(set(grouped[p])) for p in sorted(grouped.keys())}


def _prompt_pick_one(label: str, options: list[str]) -> str | None:
    """Render numbered options and accept either index or literal name.

    Returns the picked option string, or ``None`` on empty input.
    """
    if not options:
        return None
    table = Table(show_header=False, padding=(0, 1))
    table.add_column("#", style="cyan", justify="right")
    table.add_column("name")
    for i, opt in enumerate(options, start=1):
        table.add_row(str(i), opt)
    console.print(table)
    raw = typer.prompt(f"Pick a {label} (number or name)", default="")
    raw = raw.strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(options):
            return options[idx - 1]
        console.print(f"[red]Index {idx} out of range[/red]")
        return None
    if raw in options:
        return raw
    console.print(f"[red]Unknown {label}: {raw}[/red]")
    return None


def model_picker() -> None:
    """Interactive provider + model picker. Persists to active config.yaml."""
    grouped = _grouped_models()
    if not grouped:
        console.print(
            "[yellow]No models registered yet.[/yellow] Add one first with "
            "[cyan]oc models add <provider> <model>[/cyan]."
        )
        raise typer.Exit(1)

    # Show current selection so the user can keep it.
    cfg = load_config()
    current_p = cfg.model.provider
    current_m = cfg.model.model
    console.print(
        f"[dim]Current:[/dim] [cyan]{current_p}[/cyan] / "
        f"[cyan]{current_m}[/cyan]\n"
    )

    console.print("[bold]Providers:[/bold]")
    providers = list(grouped.keys())
    chosen_provider = _prompt_pick_one("provider", providers)
    if chosen_provider is None:
        console.print("[dim]Aborted.[/dim]")
        raise typer.Exit(0)

    console.print(f"\n[bold]Models for[/bold] [cyan]{chosen_provider}[/cyan]:")
    models = grouped[chosen_provider]
    chosen_model = _prompt_pick_one("model", models)
    if chosen_model is None:
        console.print("[dim]Aborted.[/dim]")
        raise typer.Exit(0)

    new_cfg = set_value(cfg, "model.provider", chosen_provider)
    new_cfg = set_value(new_cfg, "model.model", chosen_model)
    path = save_config(new_cfg)

    console.print(
        f"\n[green]✓[/green] Default model set: "
        f"[cyan]{chosen_provider}[/cyan] / [cyan]{chosen_model}[/cyan]"
    )
    console.print(f"[dim]Persisted to {path}[/dim]")


__all__ = ["model_picker"]
