"""Empty-state helpers — "the empty state IS the manual" (2026-04-28).

Pre-2026-04-28 the CLI's empty states said things like:

  $ oc cost show
  No usage recorded and no limits set.

This was a missed teaching moment: the user looking at `oc cost show`
for the first time is exactly the user who would benefit from
learning what cost tracking does, what it shows when populated, and
how to set caps.

The :func:`empty_state` helper renders a consistent panel:

* What this command shows when populated (1 sentence)
* Why it's empty right now (1 sentence)
* What to do about it (1-2 commands or actions)

Style: dim text, no panels with thick borders, no emoji clutter. The
goal is a short, dense, on-brand teach moment for first-time users
that doesn't get in the way of established users (it only fires when
the data set is genuinely empty).
"""
from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console


def render_empty_state(
    *,
    console: Console,
    title: str,
    when_populated: str,
    why_empty: str,
    next_steps: Sequence[str],
) -> None:
    """Print a four-line empty-state block in a consistent shape.

    Parameters
    ----------
    console:
        The Rich console to print on. Caller passes their existing one.
    title:
        Short label naming the data set ("Cost tracking", "Memory",
        "Episodic events", etc.).
    when_populated:
        One sentence describing what this command shows once data
        exists. The user is reading this BECAUSE the command was
        empty — telling them what they'd otherwise see is the most
        valuable thing we can do here.
    why_empty:
        One sentence explaining why it's empty right now. ("Nothing
        has been recorded yet — this fills in as you use the agent",
        "USER.md is unwritten — nothing's been saved here yet", etc.)
    next_steps:
        1-2 short lines describing what to do next. These are usually
        commands the user can copy/paste. Avoid long prose.
    """
    console.print(f"\n[bold cyan]── {title} (empty) ──[/bold cyan]")
    console.print(f"[dim]What this shows:[/dim]  {when_populated}")
    console.print(f"[dim]Why empty:[/dim]       {why_empty}")
    if next_steps:
        console.print("[dim]Next:[/dim]")
        for line in next_steps:
            console.print(f"  [cyan]›[/cyan] {line}")
    console.print()


def render_failure_with_teach(
    *,
    console: Console,
    error: str,
    feature_name: str,
    feature_purpose: str,
    fixes: Sequence[str],
) -> None:
    """Print a teaching-style error block.

    Generalizes the smart-fallback prompt pattern (PR #209): every
    error names the feature that would have helped + what to do
    about it. Replaces single-line "error: X" outputs that leave the
    user with no path forward.

    Style:

        error: <error>

          <feature_name> needs <feature_purpose>. Fix:
            › fix-1
            › fix-2

    Caller is responsible for the typer.Exit afterward.
    """
    console.print(f"[bold red]error:[/bold red] {error}")
    console.print()
    console.print(
        f"[dim]{feature_name}:[/dim] {feature_purpose}. [dim]To fix:[/dim]"
    )
    for fix in fixes:
        console.print(f"  [cyan]›[/cyan] {fix}")
    console.print()
