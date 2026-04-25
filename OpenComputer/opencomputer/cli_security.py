"""
``opencomputer security`` Typer subapp (Phase 3.G).

Subcommands::

    opencomputer security check <FILE_OR_->         # exit 0 = clean, 1 = quarantined
    opencomputer security check <FILE_OR_-> --wrap  # also print the envelope
    opencomputer security config show               # show active detector config

Designed for use in CI / shell pipelines and for manual review of
ingested-content samples flagged by audit log review. Reads from a
file path or from stdin (``-``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.security.instruction_detector import (
    InstructionDetectorConfig,
    default_detector,
)

security_app = typer.Typer(
    name="security",
    help="Prompt-injection detector + sanitizer (Phase 3.G).",
    no_args_is_help=True,
)

config_app = typer.Typer(
    name="config",
    help="Inspect detector configuration.",
    no_args_is_help=True,
)
security_app.add_typer(config_app, name="config")


def _read_input(target: str) -> str:
    """Read content from a file path, or from stdin if ``target == '-'``.

    Always returns a ``str``. Files are decoded as UTF-8 with
    ``errors='replace'`` so a binary blob doesn't crash the detector
    — non-text content still gets a verdict (probably ``False``,
    which is what we want).
    """
    if target == "-":
        return sys.stdin.read()
    path = Path(target)
    if not path.exists():
        raise typer.BadParameter(f"file not found: {target}")
    return path.read_text(encoding="utf-8", errors="replace")


@security_app.command("check")
def security_check(
    target: Annotated[
        str,
        typer.Argument(
            metavar="FILE_OR_-",
            help="Path to a file, or '-' to read from stdin.",
        ),
    ],
    wrap: Annotated[
        bool,
        typer.Option(
            "--wrap",
            help="Also print the wrapped envelope form when quarantined.",
        ),
    ] = False,
) -> None:
    """Run the detector over content; exit 0 clean, exit 1 quarantined.

    Useful in pipelines:

        cat suspicious.txt | opencomputer security check - --wrap > /dev/null \\
            || echo "QUARANTINED"
    """
    content = _read_input(target)
    det = default_detector()
    verdict = det.detect(content)

    console = Console()
    rules_str = ", ".join(verdict.triggered_rules) if verdict.triggered_rules else "(none)"
    if verdict.quarantine_recommended:
        console.print(
            f"[bold red]QUARANTINED[/bold red] "
            f"confidence={verdict.confidence:.2f} rules={rules_str}"
        )
    elif verdict.is_instruction_like:
        console.print(
            f"[yellow]suspicious (below threshold)[/yellow] "
            f"confidence={verdict.confidence:.2f} rules={rules_str}"
        )
    else:
        console.print(
            f"[green]clean[/green] confidence={verdict.confidence:.2f}"
        )

    if wrap and verdict.is_instruction_like:
        console.print()
        console.print(det.wrap(content, verdict))

    raise typer.Exit(1 if verdict.quarantine_recommended else 0)


@config_app.command("show")
def security_config_show() -> None:
    """Print the active :class:`InstructionDetectorConfig` (threshold etc.).

    Reads the *singleton* detector's config — i.e. what
    ``sanitize_external_content`` will use by default. Custom-config
    detectors constructed elsewhere are not reflected here.
    """
    cfg: InstructionDetectorConfig = default_detector().config
    console = Console()
    table = Table(title="InstructionDetectorConfig")
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("quarantine_threshold", f"{cfg.quarantine_threshold:.2f}")
    table.add_row("enabled", str(cfg.enabled))
    if cfg.extra_patterns:
        patterns_repr = "\n".join(repr(p) for p in cfg.extra_patterns)
    else:
        patterns_repr = "(none)"
    table.add_row("extra_patterns", patterns_repr)
    console.print(table)


__all__ = ["security_app"]
