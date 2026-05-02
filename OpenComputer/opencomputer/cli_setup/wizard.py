"""Wizard orchestrator — section-driven flow.

Visual + UX modeled after hermes-agent's hermes_cli/setup.py::run_setup_wizard.
Independently re-implemented (no code copied) — see spec § 10 O1.
"""
from __future__ import annotations

from pathlib import Path

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


def _gate_configured_section(ctx: WizardCtx, section_title: str) -> SectionResult | None:
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


def _all_live_sections_configured(ctx: WizardCtx) -> bool:
    """Q1 — True if every LIVE (non-deferred) section's configured_check
    returns True. Used to detect 'fully set up' state on re-run."""
    for section in _sections.SECTION_REGISTRY:
        if section.deferred:
            continue
        if section.configured_check is None:
            continue  # sections without a check can't report state
        if not _safe_configured_check(section, ctx):
            return False
    return True


def _offer_full_reconfigure(ctx: WizardCtx) -> bool:
    """Q1 — when the wizard re-runs and everything is configured, ask
    'reconfigure all / skip wizard'. Returns True if user chose to skip
    (caller should short-circuit), False to fall through to per-section gating."""
    choices = [
        Choice("Walk through every section (re-prompt as needed)", "walk"),
        Choice("Skip — config looks complete (use existing values)", "skip"),
    ]
    idx = radiolist(
        "OpenComputer is already fully configured — what would you like to do?",
        choices, default=1,
    )
    return idx == 1


def run_setup(
    *,
    quick: bool = False,
    non_interactive: bool = False,
) -> int:
    """Top-level wizard entry. Returns exit code (0 = ok, 1 = cancelled).
    Always returns; never raises.

    ``non_interactive=True`` (Q2) short-circuits all interactive
    prompts: configured-checks defer to existing state, unconfigured
    sections skip with a default-or-skip behavior. Useful for CI /
    headless invocations where prompts would hang.
    """
    config_path = _resolve_config_path()
    config, is_first_run = _load_config(config_path)

    ctx = WizardCtx(
        config=config,
        config_path=config_path,
        is_first_run=is_first_run,
        quick_mode=quick,
    )
    ctx.extra["non_interactive"] = non_interactive

    _print_header()

    # Q1 — detect "everything is configured" state and offer a global
    # skip. Skipped on first run (nothing to skip) and in
    # non_interactive mode (no prompts).
    if not is_first_run and not non_interactive and _all_live_sections_configured(ctx):
        try:
            if _offer_full_reconfigure(ctx):
                _print_setup_summary(ctx)
                _console.print(
                    "\n[green]✓ Skipped — config already complete.[/green]"
                )
                return 0
        except WizardCancelled:
            _console.print(
                "\n[red]Setup cancelled.[/red] Run `oc setup` again to retry."
            )
            return 1

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
                    if non_interactive:
                        # Q2 — keep existing config; no prompt
                        _print_section_footer(SectionResult.SKIPPED_KEEP)
                        continue
                    gated = _gate_configured_section(ctx, section.title)
                    if gated is not None:
                        _print_section_footer(gated)
                        continue

                if non_interactive:
                    # Q2 — fresh section without a default → skip
                    _console.print(
                        "  [dim](non-interactive — skipped)[/dim]"
                    )
                    _print_section_footer(SectionResult.SKIPPED_FRESH)
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
    _print_setup_summary(ctx)
    _console.print("\n[green]✓ Setup complete.[/green] Run `oc chat` to start.")
    return 0


def _print_setup_summary(ctx: WizardCtx) -> None:
    """Print a Hermes-style configuration summary block after the section
    loop. Walks ctx.config and reports configured / missing pieces with
    brief next-step hints.

    Modeled after hermes_cli/setup.py::_print_setup_summary (line 348).
    Independently re-implemented (no code copied) — checks OC's config
    shape rather than Hermes's.
    """
    cfg = ctx.config
    _console.print("\n[bold cyan]◆ Configuration Summary[/bold cyan]")

    rows: list[tuple[str, bool, str]] = []  # (label, ok, hint)

    # Inference provider
    model = cfg.get("model") or {}
    provider = model.get("provider") or ""
    if provider and provider != "none":
        rows.append((f"Inference provider: {provider}", True, ""))
    else:
        rows.append(("Inference provider", False, "run `oc setup --new` to select"))

    # Messaging platforms
    platforms = (cfg.get("gateway") or {}).get("platforms") or []
    if platforms:
        rows.append((f"Messaging platforms: {', '.join(platforms)}", True, ""))
    else:
        rows.append(("Messaging platforms", False, "skipped — set up later via the wizard"))

    # Agent settings
    loop = cfg.get("loop") or {}
    if loop.get("max_iterations"):
        rows.append(
            (f"Agent settings: max_iterations={loop['max_iterations']}", True, ""),
        )
    else:
        rows.append(("Agent settings", False, "using built-in defaults"))

    # TTS
    tts = cfg.get("tts") or {}
    if tts.get("provider"):
        rows.append((f"TTS: {tts['provider']}", True, ""))
    else:
        rows.append(("TTS", False, "skipped — voice output not configured"))

    # Terminal backend
    terminal = cfg.get("terminal") or {}
    if terminal.get("backend"):
        rows.append((f"Terminal backend: {terminal['backend']}", True, ""))
    else:
        rows.append(("Terminal backend", False, "using default (local)"))

    # Tools / plugins
    enabled_plugins = (cfg.get("plugins") or {}).get("enabled") or []
    if enabled_plugins:
        plug_label = ", ".join(enabled_plugins[:3])
        if len(enabled_plugins) > 3:
            plug_label += f" + {len(enabled_plugins) - 3} more"
        rows.append((f"Plugins: {plug_label}", True, ""))
    else:
        rows.append(("Plugins", False, "no plugin preset applied"))

    # Launchd service
    launchd_installed = (cfg.get("gateway") or {}).get("launchd_installed")
    if launchd_installed:
        rows.append(("Launchd service: installed", True, ""))
    # else: omit — not relevant on non-macOS, and "skipped" is the default

    # Migrations
    migrations = (cfg.get("migrations") or {}).get("prior_install") or []
    if migrations:
        sources = ", ".join(m.get("source", "?") for m in migrations)
        rows.append((f"Imported from: {sources}", True, ""))

    # Render
    for label, ok, hint in rows:
        glyph = "[green]✓[/green]" if ok else "[yellow]·[/yellow]"
        line = f"  {glyph} {label}"
        if not ok and hint:
            line += f"  [dim]({hint})[/dim]"
        _console.print(line)

    # Config location pointer
    _console.print(
        f"\n  [dim]Config: {ctx.config_path}[/dim]"
    )
