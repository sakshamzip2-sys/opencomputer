"""Format goal-loop banners (achieved / continue / pause_budget).

Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §3 Gap B.

The strings are surfaced into the streaming console output by the input
loop / gateway dispatch via :attr:`AgentLoop.goal_banner_callback`. The
formatter is pure — separating formatting from the printing site keeps
the CLI banner reusable in tests and lets the gateway adapter render
the same lines into channel messages without touching ``Console``.
"""

from __future__ import annotations

from opencomputer.agent.goal import GoalState, JudgeVerdict


def format_banner(
    *, kind: str, verdict: JudgeVerdict, goal: GoalState,
) -> str:
    """Return the rich-markup string for a loop banner.

    Args:
        kind: One of ``"continue"``, ``"achieved"``, ``"pause_budget"``.
            Unknown kinds default to the continue rendering — failing
            soft is appropriate for a UX-only path.
        verdict: The :class:`JudgeVerdict` whose ``reason`` populates the
            banner.
        goal: The current :class:`GoalState`. ``turns_used`` and
            ``budget`` drive the ``N/M`` counter.

    Returns:
        Rich-markup string (caller passes to ``console.print``).
    """
    if kind == "achieved":
        return f"[green]✓ Goal achieved:[/green] {verdict.reason}"
    if kind == "pause_budget":
        return (
            f"[yellow]⏸ Goal paused — {goal.turns_used}/{goal.budget} "
            "turns used. Use [cyan]/goal resume[/cyan] to keep going, or "
            "[cyan]/goal clear[/cyan] to stop.[/yellow]"
        )
    # default: continue
    return (
        f"[cyan]↻ Continuing toward goal "
        f"({goal.turns_used}/{goal.budget}):[/cyan] {verdict.reason}"
    )
