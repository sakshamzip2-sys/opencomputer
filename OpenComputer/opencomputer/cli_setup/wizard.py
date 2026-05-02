"""Wizard orchestrator — section-driven flow.

Visual + UX modeled after hermes-agent's hermes_cli/setup.py::run_setup_wizard.
Independently re-implemented (no code copied) — see spec § 10 O1.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.panel import Panel

from opencomputer.cli_setup import sections as _sections
from opencomputer.cli_setup.sections import (
    SectionResult,
    WizardCtx,
    WizardSection,
)
from opencomputer.cli_ui.menu import Choice, WizardCancelled, radiolist

__all__ = ["WizardCancelled", "run_setup"]

_console = Console()


def _resolve_config_path() -> Path:
    """Return path to the active profile's config.yaml.

    Delegates to opencomputer.agent.config_store.config_file_path() —
    the canonical resolver that already accounts for OPENCOMPUTER_HOME
    + per-profile overrides. Wrapper exists so tests can monkeypatch
    just this name without touching every config_store consumer.
    """
    from opencomputer.agent.config_store import config_file_path
    return config_file_path()


def _load_config(path: Path) -> tuple[dict, bool]:
    """Returns (config_dict, is_first_run)."""
    if not path.exists():
        return {}, True
    try:
        return yaml.safe_load(path.read_text()) or {}, False
    except Exception:  # noqa: BLE001
        return {}, True


def _save_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False))


def _print_header() -> None:
    _console.print(Panel(
        "Let's configure your OpenComputer installation.\n"
        "Press Ctrl+C at any time to exit.",
        title="✦ OpenComputer Setup Wizard",
        border_style="magenta",
    ))


def _print_section_header(icon: str, title: str, description: str) -> None:
    _console.print(f"\n[bold cyan]{icon} {title}[/bold cyan]")
    for line in description.splitlines():
        _console.print(f"  [dim]{line}[/dim]")


def _print_section_footer(result: SectionResult) -> None:
    msg = {
        SectionResult.CONFIGURED: "[green]✓ Configured[/green]",
        SectionResult.SKIPPED_KEEP: "[dim]Skipped (keeping current)[/dim]",
        SectionResult.SKIPPED_FRESH: "[dim]Skipped[/dim]",
        SectionResult.CANCELLED: "[red]✗ Cancelled[/red]",
    }[result]
    _console.print(f"  {msg}")


def _gate_configured_section(ctx: WizardCtx, section_title: str) -> Optional[SectionResult]:
    """When the section reports configured, ask keep / reconfigure / skip.
    Returns:
      - SectionResult.SKIPPED_KEEP / SectionResult.SKIPPED_FRESH if user chose
        not to invoke the handler
      - None if user chose to reconfigure (caller should invoke handler)
    """
    choices = [
        Choice("Keep current", "keep"),
        Choice("Reconfigure", "reconfigure"),
        Choice("Skip", "skip"),
    ]
    idx = radiolist(
        f"{section_title} is already configured — what would you like to do?",
        choices, default=0,
    )
    if idx == 0:
        return SectionResult.SKIPPED_KEEP
    if idx == 1:
        return None
    return SectionResult.SKIPPED_FRESH


def _safe_configured_check(section: WizardSection, ctx: WizardCtx) -> bool:
    """Call section.configured_check defensively. Bug in handler must not crash wizard."""
    if section.configured_check is None:
        return False
    try:
        return bool(section.configured_check(ctx))
    except Exception as exc:  # noqa: BLE001
        _console.print(
            f"  [yellow]⚠ configured_check raised {type(exc).__name__} — "
            f"treating as 'not configured'[/yellow]"
        )
        return False


def run_setup(*, quick: bool = False) -> int:
    """Top-level wizard entry. Returns exit code (0 = ok, 1 = cancelled).
    Always returns; never raises."""
    config_path = _resolve_config_path()
    config, is_first_run = _load_config(config_path)

    ctx = WizardCtx(
        config=config,
        config_path=config_path,
        is_first_run=is_first_run,
        quick_mode=quick,
    )

    _print_header()

    try:
        for section in _sections.SECTION_REGISTRY:
            _print_section_header(section.icon, section.title, section.description)

            if section.deferred:
                target = section.target_subproject or "future sub-project"
                _console.print(
                    f"  [dim](deferred — coming in sub-project {target})[/dim]"
                )
                _print_section_footer(SectionResult.SKIPPED_FRESH)
                continue

            try:
                if _safe_configured_check(section, ctx):
                    gated = _gate_configured_section(ctx, section.title)
                    if gated is not None:
                        _print_section_footer(gated)
                        continue

                result = section.handler(ctx)
                _print_section_footer(result)
            except WizardCancelled:
                _print_section_footer(SectionResult.CANCELLED)
                _console.print(
                    "\n[red]Setup cancelled.[/red] Run `oc setup` again to retry."
                )
                return 1
    except KeyboardInterrupt:
        _console.print(
            "\n[red]Setup interrupted (Ctrl+C).[/red] Run `oc setup` again to retry."
        )
        return 1

    _save_config(config_path, ctx.config)
    _console.print("\n[green]✓ Setup complete.[/green] Run `oc chat` to start.")
    return 0
