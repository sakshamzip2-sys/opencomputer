"""``oc help`` — opt-in guided experiences for users who want a walkthrough.

The default OpenComputer onboarding is *passive* (learning-moments,
empty-states-as-manual). For users who explicitly want a guided tour
of the capability surface, this command surfaces a curated 7-step
walkthrough that demonstrates the key features without being
interactive (no questions, no waiting).

Design principles:

* **Opt-in only.** Never auto-runs. Never suggested. The user must
  explicitly type ``oc help tour``.
* **Read-only demo.** No state changes — just print + show. The
  reader can re-run any step manually after seeing it.
* **One screen per step.** The whole tour fits in one ``less``-able
  output if redirected, but renders well live too.
* **Sequenced for first-pass coverage.** identity → memory → vibe →
  cost → skills → plugins → next steps.
"""
from __future__ import annotations

import typer
from rich.console import Console

help_app = typer.Typer(
    name="help",
    help="Guided overviews + first-pass walkthroughs.",
    no_args_is_help=True,
)
_console = Console()


_TOUR_STEPS: tuple[tuple[str, str, str], ...] = (
    (
        "1 — Identity & profile",
        "OpenComputer is a personal AI agent on your machine. It has tools, "
        "memory across sessions, and a registry of skills. The first thing it "
        "needs is a sense of who you are — that lives in USER.md.",
        "oc memory edit --user   →   open USER.md in your editor",
    ),
    (
        "2 — Persistent memory",
        "MEMORY.md is the agent's scratch — what's currently true, what to "
        "remember next session. Either edit it directly or, in chat, say "
        "'remember that X' and the agent appends.",
        "oc memory show          →   see what's in MEMORY.md right now",
    ),
    (
        "3 — Vibe + emotional tracking",
        "The agent classifies the felt tone of each turn (calm / curious / "
        "stuck / frustrated / excited / tired). It uses this to soften "
        "responses when you sound stuck and to recall continuity (\"you "
        "sounded frustrated last time we talked\").",
        "oc memory show vibe     →   per-turn vibe history (after some use)",
    ),
    (
        "4 — Cost tracking + caps",
        "Every API call is recorded. Set daily/monthly caps so a runaway "
        "deepening pass on Anthropic / OpenAI can't burn $50 silently. Ollama "
        "is free and bypasses the cost guard entirely.",
        "oc cost show            →   current spend\n     oc cost set-limit --provider anthropic --daily 5",
    ),
    (
        "5 — Skills (named recipes)",
        "A skill is a Markdown file the agent can invoke directly when its "
        "trigger matches. Plugins ship them; you can also author your own. "
        "When you hit a recurring task, the agent can save it as a new skill "
        "(opt-in via auto-skill-evolution).",
        "oc skills               →   list available skills",
    ),
    (
        "6 — Plugins (extension points)",
        "Channels (Telegram, Discord, Slack, …), providers (Anthropic, "
        "OpenAI, …), tools, and memory providers all ship as plugins. "
        "Most are bundled; install more from PyPI.",
        "oc plugins              →   list discovered plugins",
    ),
    (
        "7 — Where to go next",
        "Run a real chat. The agent's discoverability layer (\"learning "
        "moments\") will surface relevant features inline as you work — at "
        "most one tip per day, never the same tip twice. To opt out: "
        "`oc memory learning-off`.",
        "oc chat                 →   start chatting\n     oc doctor              →   diagnose env issues",
    ),
)


@help_app.command("tour")
def help_tour() -> None:
    """Print a 7-step capability walkthrough.

    Read-only — no state changes. Designed for first-time users who
    want a guided introduction to OpenComputer's main surfaces. The
    passive learning-moments system handles the in-flow discovery for
    everyone else.
    """
    _console.print()
    _console.print(
        "[bold cyan]Welcome to OpenComputer.[/bold cyan]  "
        "[dim]7-step tour follows. Each step is a feature you can "
        "explore on your own afterward.[/dim]"
    )
    _console.print()

    for title, body, command_hint in _TOUR_STEPS:
        _console.print(f"[bold cyan]── {title} ──[/bold cyan]")
        _console.print(f"  {body}")
        _console.print(f"  [dim]Try:[/dim]  [cyan]{command_hint}[/cyan]")
        _console.print()

    _console.print(
        "[dim]Tour complete. The full reference is in the README; "
        "in-context hints surface as you use the agent.[/dim]"
    )
    _console.print()
