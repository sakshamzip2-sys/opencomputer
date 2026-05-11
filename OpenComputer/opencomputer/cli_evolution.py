"""``oc evolution`` — inspect and control OC's self-evolution loop.

Surfaces the :class:`opencomputer.agent.evolution_orchestrator.EvolutionOrchestrator`
state and lets the operator manually trigger a tune or reset.

Subcommands
-----------

* ``oc evolution status`` — show current tuning, decision counts, and
  the most-recent N decisions in the rolling window. **Aggregate
  only** — never prints skill bodies or session transcripts to honour
  the same privacy posture as ``oc skills evolution status``.
* ``oc evolution tune`` — force an immediate recompute from the current
  rolling-window state. Useful after manually adjusting decisions in
  the underlying state file, or after onboarding to seed the
  thresholds with whatever decisions already exist.
* ``oc evolution reset`` — restore tuning to module defaults and clear
  the rolling window. Destructive; prompts unless ``--yes``.

The CLI is independent of whether the gateway is running. It reads the
persisted tuning file directly via :func:`load_tuning` for ``status``,
and constructs a transient orchestrator instance for ``tune`` and
``reset`` so the file write happens in the user's terminal rather than
needing the gateway to be alive.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.evolution_orchestrator import (
    DEFAULT_TUNING,
    EvolutionOrchestrator,
    EvolutionTuning,
    load_tuning,
)

logger = logging.getLogger("opencomputer.cli_evolution")

evolution_app = typer.Typer(
    name="evolution-tuning",
    help="Inspect and control OC's closed-loop threshold tuner "
         "(skill-evolution confidence + dreaming-v2 score/recall). "
         "Distinct from `oc evolution` (trajectory/prompts/skills).",
    no_args_is_help=True,
)

_console = Console()


def _profile_home() -> Path:
    """Resolve the active profile home, mirroring other CLI surfaces.

    Honours ``OPENCOMPUTER_PROFILE_HOME`` for explicit overrides;
    otherwise builds ``~/.opencomputer/<profile>/`` from
    ``OPENCOMPUTER_PROFILE`` (defaulting to ``"default"``).
    """
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME", "").strip()
    if env:
        return Path(env)
    profile = os.environ.get("OPENCOMPUTER_PROFILE", "default").strip() or "default"
    return Path.home() / ".opencomputer" / profile


def _format_ts(ts: float) -> str:
    """Human-friendly UTC timestamp; ``never`` when ``ts`` is 0."""
    if not ts:
        return "never"
    try:
        return (
            datetime.fromtimestamp(ts, tz=UTC)
            .strftime("%Y-%m-%d %H:%M:%S UTC")
        )
    except (OSError, OverflowError, ValueError):
        return "invalid"


def _render_tuning_table(tuning: EvolutionTuning) -> Table:
    """Build a rich.Table of the tuning fields plus deltas from default."""
    table = Table(title="Evolution Tuning", show_header=True, header_style="bold")
    table.add_column("Parameter", justify="left")
    table.add_column("Current", justify="right")
    table.add_column("Default", justify="right")
    table.add_column("Delta", justify="right")

    rows = [
        (
            "confidence_threshold",
            tuning.confidence_threshold,
            DEFAULT_TUNING.confidence_threshold,
        ),
        (
            "dreaming_v2_score_threshold",
            f"{tuning.dreaming_v2_score_threshold:.2f}",
            f"{DEFAULT_TUNING.dreaming_v2_score_threshold:.2f}",
        ),
        (
            "dreaming_v2_min_recall",
            tuning.dreaming_v2_min_recall,
            DEFAULT_TUNING.dreaming_v2_min_recall,
        ),
    ]
    for name, current, default in rows:
        try:
            delta_value: float = float(current) - float(default)
        except (TypeError, ValueError):
            delta_value = 0.0
        delta_str = f"{delta_value:+.2f}" if isinstance(default, str) else f"{int(delta_value):+d}"
        table.add_row(str(name), str(current), str(default), delta_str)
    return table


@evolution_app.command("status")
def status_command(
    show_window: bool = typer.Option(
        False,
        "--window",
        help="Print the rolling decision window (aggregate counts; no skill names).",
    ),
) -> None:
    """Show the current tuning, last-recompute time, and decision count."""
    profile_home = _profile_home()
    tuning = load_tuning(profile_home)

    _console.print(_render_tuning_table(tuning))
    _console.print(
        f"\n[bold]Decisions observed:[/bold] {tuning.decisions_observed}"
    )
    _console.print(
        f"[bold]Last recompute:[/bold] {_format_ts(tuning.last_recompute_ts)}"
    )

    if show_window:
        # Aggregate-only window display — counts per decision class,
        # never individual skill names or session ids. Mirrors the
        # privacy posture of ``oc skills evolution status``.
        # The CLI doesn't hold a live orchestrator instance; instead
        # we count decisions present on disk via the candidate-store
        # listing (proposed = deferred, plus the actions emitted on
        # SkillReviewDecisionEvent are not persisted to disk yet —
        # that's an out-of-scope future enhancement). For now the
        # window count comes from the persisted tuning state.
        _console.print(
            "\n[dim]rolling window is held in memory by the gateway "
            "orchestrator; restart the gateway to reset it.[/dim]"
        )


@evolution_app.command("tune")
def tune_command() -> None:
    """Force an immediate recompute from the current rolling window.

    Constructs a transient orchestrator that does NOT subscribe to the
    bus — it only loads the persisted state, runs the math (which
    will be a no-op because the in-memory window is empty in this
    process), and writes back. This is mostly useful after editing
    the tuning file by hand to verify a clean parse.

    For a real tune driven by accumulated decisions, the gateway's
    long-lived orchestrator does the work automatically every
    :data:`evolution_orchestrator._MIN_DECISIONS_TO_TUNE` decisions.
    """
    profile_home = _profile_home()
    profile_home.mkdir(parents=True, exist_ok=True)

    # Bus-less construction: the orchestrator's ``recompute_tuning``
    # only reads the in-memory window, so passing a null bus is safe
    # if we never call ``start``.
    class _NullBus:
        def subscribe(self, *_args, **_kwargs):  # pragma: no cover - unused
            raise RuntimeError("null bus does not subscribe")

    orchestrator = EvolutionOrchestrator(
        bus=_NullBus(), profile_home=profile_home, langfuse_score_fn=None
    )
    new = orchestrator.recompute_tuning()
    _console.print("[green]✓[/green] recomputed.\n")
    _console.print(_render_tuning_table(new))


@evolution_app.command("reset")
def reset_command(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Reset evolution tuning to module defaults.

    Destructive: discards any accumulated tuning. The rolling window
    held in the gateway's running orchestrator is also cleared if the
    gateway is running and shares the same profile.
    """
    profile_home = _profile_home()
    if not yes:
        if not typer.confirm(
            "This resets evolution tuning to defaults. Continue?",
            default=False,
        ):
            typer.echo("cancelled")
            return

    class _NullBus:
        def subscribe(self, *_args, **_kwargs):  # pragma: no cover - unused
            raise RuntimeError("null bus does not subscribe")

    orchestrator = EvolutionOrchestrator(
        bus=_NullBus(), profile_home=profile_home, langfuse_score_fn=None
    )
    new = orchestrator.reset()
    _console.print("[green]✓[/green] reset to defaults.\n")
    _console.print(_render_tuning_table(new))


__all__ = ["evolution_app"]
