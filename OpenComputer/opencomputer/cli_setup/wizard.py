"""OC-branded Hermes-style setup wizard."""
from __future__ import annotations

from contextlib import nullcontext
import os
import logging
import subprocess
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel

from opencomputer.cli_setup import sections as _sections
from opencomputer.cli_setup.env_writer import default_env_file, read_env_value
from opencomputer.cli_setup.sections import SectionResult, WizardCtx, WizardSection
from opencomputer.cli_ui.menu import Choice, WizardCancelled, radiolist

__all__ = ["WizardCancelled", "run_setup"]


def _prepare_output_encoding() -> None:
    """Avoid UnicodeEncodeError on legacy Windows code pages.

    Rich and plain section handlers both write Hermes-style glyphs. Modern
    terminals render them; old cp1252 streams cannot encode them. Replacing
    unsupported glyphs is better than crashing setup.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass


_prepare_output_encoding()
_console = Console()


def _resolve_config_path() -> Path:
    from opencomputer.agent.config_store import config_file_path

    return config_file_path()


def _load_config(path: Path) -> tuple[dict, bool]:
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


def _clear_interactive_screen(non_interactive: bool = False) -> None:
    if non_interactive:
        return
    _console.clear()


def _wizard_screen(non_interactive: bool = False):
    if non_interactive:
        return nullcontext()
    return _console.screen()


def _prompt_setup_mode() -> bool:
    choices = [
        Choice(
            "Quick setup - provider, model & messaging (recommended)",
            "quick",
        ),
        Choice("Full setup - configure everything", "full"),
    ]
    idx = radiolist("How would you like to set up OpenComputer?", choices, default=0)
    return idx == 0


def _print_section_header(icon: str, title: str, description: str) -> None:
    _console.print(f"\n[bold cyan]{icon} {title}[/bold cyan]")
    for line in description.splitlines():
        _console.print(f"  [dim]{line}[/dim]")


def _print_section_footer(result: SectionResult) -> None:
    msg = {
        SectionResult.CONFIGURED: "[green]✓ Configured[/green]",
        SectionResult.SKIPPED_KEEP: "[dim]Skipped (keeping current)[/dim]",
        SectionResult.SKIPPED_FRESH: "[dim]Skipped[/dim]",
        SectionResult.CANCELLED: "[red]× Cancelled[/red]",
    }[result]
    _console.print(f"  {msg}")


def _gate_configured_section(ctx: WizardCtx, section_title: str) -> SectionResult | None:
    choices = [
        Choice("Keep current", "keep"),
        Choice("Reconfigure", "reconfigure"),
        Choice("Skip", "skip"),
    ]
    idx = radiolist(
        f"{section_title} is already configured - what would you like to do?",
        choices,
        default=0,
    )
    if idx == 0:
        return SectionResult.SKIPPED_KEEP
    if idx == 1:
        return None
    return SectionResult.SKIPPED_FRESH


def _safe_configured_check(section: WizardSection, ctx: WizardCtx) -> bool:
    if section.configured_check is None:
        return False
    try:
        return bool(section.configured_check(ctx))
    except Exception as exc:  # noqa: BLE001
        _console.print(
            f"  [yellow]⚠ configured_check raised {type(exc).__name__}; "
            "treating as not configured[/yellow]"
        )
        return False


def _all_live_sections_configured(ctx: WizardCtx) -> bool:
    for section in _sections.SECTION_REGISTRY:
        if section.deferred or section.configured_check is None:
            continue
        if not _safe_configured_check(section, ctx):
            return False
    return True


def _offer_full_reconfigure(ctx: WizardCtx) -> bool:
    choices = [
        Choice("Walk through every section", "walk"),
        Choice("Skip - config looks complete", "skip"),
    ]
    idx = radiolist(
        "OpenComputer is already fully configured - what would you like to do?",
        choices,
        default=1,
    )
    return idx == 1


def run_setup(
    *,
    quick: bool | None = False,
    non_interactive: bool = False,
) -> int:
    """Run the setup wizard. Returns 0 on success, 1 on cancellation."""
    discovery_logger = logging.getLogger("opencomputer.plugins.discovery")
    previous_discovery_level = discovery_logger.level
    discovery_logger.setLevel(logging.ERROR)
    screen_cm = _wizard_screen(non_interactive)
    screen_cm.__enter__()
    screen_closed = False

    def restore_discovery_logger() -> None:
        nonlocal screen_closed
        discovery_logger.setLevel(previous_discovery_level)
        if not screen_closed:
            screen_closed = True
            screen_cm.__exit__(None, None, None)

    try:
        config_path = _resolve_config_path()
        config, is_first_run = _load_config(config_path)

        _clear_interactive_screen(non_interactive)
        _print_header()
        if quick is None and not non_interactive:
            try:
                quick = _prompt_setup_mode()
            except WizardCancelled:
                _console.print("\n[red]Setup cancelled.[/red] Run `oc setup` again to retry.")
                restore_discovery_logger()
                return 1
        quick = bool(quick)

        ctx = WizardCtx(
            config=config,
            config_path=config_path,
            is_first_run=is_first_run,
            quick_mode=quick,
        )
        ctx.extra["non_interactive"] = non_interactive

        try:
            for section in _sections.SECTION_REGISTRY:
                if quick and not section.quick:
                    continue

                _clear_interactive_screen(non_interactive)
                _print_section_header(section.icon, section.title, section.description)

                if section.deferred:
                    target = section.target_subproject or "future sub-project"
                    _console.print(f"  [dim](deferred - coming in {target})[/dim]")
                    _print_section_footer(SectionResult.SKIPPED_FRESH)
                    continue

                if _safe_configured_check(section, ctx):
                    if non_interactive:
                        _print_section_footer(SectionResult.SKIPPED_KEEP)
                        continue
                    gated = _gate_configured_section(ctx, section.title)
                    if gated is not None:
                        _print_section_footer(gated)
                        continue

                if non_interactive:
                    _console.print("  [dim](non-interactive - skipped)[/dim]")
                    _print_section_footer(SectionResult.SKIPPED_FRESH)
                    continue

                try:
                    result = section.handler(ctx)
                except WizardCancelled:
                    _print_section_footer(SectionResult.CANCELLED)
                    _console.print("\n[red]Setup cancelled.[/red] Run `oc setup` again to retry.")
                    restore_discovery_logger()
                    return 1
                _print_section_footer(result)
        except KeyboardInterrupt:
            _console.print(
                "\n[red]Setup interrupted (Ctrl+C).[/red] Run `oc setup` again to retry."
            )
            restore_discovery_logger()
            return 1

        _save_config(config_path, ctx.config)
        _clear_interactive_screen(non_interactive)
        _print_setup_summary(ctx)
        _maybe_launch_chat(ctx)
        restore_discovery_logger()
        return 0
    except BaseException:
        restore_discovery_logger()
        raise


def _env_present(*names: str) -> bool:
    for name in names:
        if os.environ.get(name) or read_env_value(name):
            return True
    return False


def _tool_rows(ctx: WizardCtx) -> list[tuple[str, bool, str]]:
    cfg = ctx.config
    rows = [
        (
            "Vision (image analysis)",
            bool((cfg.get("vision") or {}).get("provider")) or _env_present("OPENAI_API_KEY"),
            "missing run `oc setup` to configure",
        ),
        (
            "Mixture of Agents",
            _env_present("OPENROUTER_API_KEY"),
            "missing OPENROUTER_API_KEY",
        ),
        (
            "Web Search & Extract",
            _env_present("EXA_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY", "SEARXNG_URL"),
            "missing EXA_API_KEY, FIRECRAWL_API_KEY, TAVILY_API_KEY, or SEARXNG_URL",
        ),
        ("Browser Automation", True, ""),
        (
            "Image Generation",
            _env_present("FAL_KEY", "OPENAI_API_KEY"),
            "missing FAL_KEY or OPENAI_API_KEY",
        ),
        (
            "Text-to-Speech",
            bool((cfg.get("tts") or {}).get("provider")),
            "not configured",
        ),
        ("Terminal/Commands", True, ""),
        ("Task Planning", True, ""),
        ("Skills", True, ""),
    ]
    return rows


def _print_setup_summary(ctx: WizardCtx) -> None:
    cfg = ctx.config

    _console.print("\n[bold cyan]◆ Configuration Summary[/bold cyan]")
    model = cfg.get("model") or {}
    provider = model.get("provider") or ""
    if provider and provider != "none":
        _console.print(f"  [green]✓[/green] Inference provider: {provider}")
    else:
        _console.print("  [yellow]·[/yellow] Inference provider [dim](run `oc setup` to select)[/dim]")

    platforms = (cfg.get("gateway") or {}).get("platforms") or []
    if platforms:
        _console.print(f"  [green]✓[/green] Messaging platforms: {', '.join(platforms)}")
    else:
        _console.print("  [yellow]·[/yellow] Messaging platforms [dim](configure later)[/dim]")

    loop = cfg.get("loop") or {}
    if loop.get("max_iterations"):
        _console.print(f"  [green]✓[/green] Agent settings: max_iterations={loop['max_iterations']}")
    else:
        _console.print("  [yellow]·[/yellow] Agent settings [dim](using defaults)[/dim]")

    tts = cfg.get("tts") or {}
    if tts.get("provider"):
        _console.print(f"  [green]✓[/green] TTS: {tts['provider']}")
    else:
        _console.print("  [yellow]·[/yellow] TTS [dim](not configured)[/dim]")

    terminal = cfg.get("terminal") or {}
    if terminal.get("backend"):
        _console.print(f"  [green]✓[/green] Terminal backend: {terminal['backend']}")
    else:
        _console.print("  [yellow]·[/yellow] Terminal backend [dim](using local)[/dim]")

    enabled_plugins = (cfg.get("plugins") or {}).get("enabled") or []
    if enabled_plugins:
        plug_label = ", ".join(enabled_plugins[:3])
        if len(enabled_plugins) > 3:
            plug_label += f" + {len(enabled_plugins) - 3} more"
        _console.print(f"  [green]✓[/green] Plugins: {plug_label}")
    else:
        _console.print("  [yellow]·[/yellow] Plugins [dim](no preset applied)[/dim]")

    _console.print("\n[bold cyan]◆ Tool Availability Summary[/bold cyan]")
    rows = _tool_rows(ctx)
    available = sum(1 for _, ok, _ in rows if ok)
    _console.print(f"  [dim]{available}/{len(rows)} tool categories available:[/dim]\n")
    for label, ok, hint in rows:
        glyph = "[green]✓[/green]" if ok else "[red]×[/red]"
        line = f"  {glyph} {label}"
        if not ok and hint:
            line += f" [dim]({hint})[/dim]"
        _console.print(line)

    if available != len(rows):
        _console.print(
            "\n[yellow]⚠ Some tools are disabled. Run `oc setup` to configure them,"
            "\n  or edit ~/.opencomputer/.env directly to add missing API keys.[/yellow]"
        )

    _console.print()
    _console.print(Panel("✓ Setup Complete!", border_style="green"))

    home = ctx.config_path.parent
    _console.print("\n[bold cyan]📁 All your files are in ~/.opencomputer/:[/bold cyan]\n")
    _console.print(f"  [yellow]Settings:[/yellow] {ctx.config_path}")
    _console.print(f"  [yellow]API Keys:[/yellow] {default_env_file()}")
    _console.print(f"  [yellow]Data:[/yellow]     {home}")

    _console.print("\n[bold cyan]📝 To edit your configuration:[/bold cyan]\n")
    commands = [
        ("oc setup", "Re-run the setup wizard"),
        ("oc model", "Change model/provider"),
        ("oc gateway setup", "Configure messaging"),
        ("oc config", "View current settings"),
        ("oc config edit", "Open config in your editor"),
        ("oc config set <key> <value>", "Set a specific value"),
    ]
    for cmd, desc in commands:
        _console.print(f"  [green]{cmd:<28}[/green] {desc}")

    _console.print("\n[bold cyan]🚀 Ready to go![/bold cyan]\n")
    ready = [
        ("oc chat", "Start chatting"),
        ("oc gateway", "Start messaging gateway"),
        ("oc doctor", "Check for issues"),
    ]
    for cmd, desc in ready:
        _console.print(f"  [green]{cmd:<16}[/green] {desc}")


def _maybe_launch_chat(ctx: WizardCtx) -> None:
    if ctx.extra.get("non_interactive") or not sys.stdin.isatty():
        return
    try:
        raw = input("\nLaunch OpenComputer chat now? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if raw not in ("", "y", "yes"):
        return
    subprocess.run([sys.executable, "-m", "opencomputer.cli", "chat"], check=False)
