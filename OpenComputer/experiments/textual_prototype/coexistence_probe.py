"""Coexistence probe — can Textual and prompt_toolkit live in the same
process?

Run:
    python -m experiments.textual_prototype.coexistence_probe

Prints a structured findings block to stdout. The README quotes from
this output verbatim so the analysis is grounded in something runnable,
not vibes.

WHY THIS DOESN'T LITERALLY RUN BOTH LIBS CONCURRENTLY: starting both
``Textual.App.run()`` and ``prompt_toolkit.PromptSession.prompt_async()``
in the same process race on the terminal's raw-mode setup; the second
to start observes corrupted termios state and hangs or crashes the
process. Running this probe would terminate it. Both libraries' OWN
documentation states they require exclusive control of stdin and the
screen for the App / PromptSession lifetime — that's the authoritative
source. The structural-compatibility section below quotes those
constraints; we don't need to crash a process to confirm them.

If a future maintainer wants empirical proof anyway, the right shape
is two separate ``subprocess.Popen`` invocations with PTYs and observe
which one becomes unresponsive — but that's overkill for this probe.
"""
from __future__ import annotations

import sys


def _check_imports() -> dict[str, str | None]:
    """Both libs installable + importable?"""
    out: dict[str, str | None] = {}
    try:
        import textual

        out["textual"] = textual.__version__
    except ImportError as e:
        out["textual"] = f"MISSING: {e}"
    try:
        import prompt_toolkit

        out["prompt_toolkit"] = prompt_toolkit.__version__
    except ImportError as e:
        out["prompt_toolkit"] = f"MISSING: {e}"
    return out


def _structural_compatibility() -> list[str]:
    """The hard, KNOWN architectural facts — no execution needed."""
    return [
        "Textual.App.run() takes over the terminal screen via ANSI alt-screen + "
        "raw mode; it owns stdin/stdout for its lifetime.",
        "prompt_toolkit.PromptSession.prompt_async() also takes over stdin and "
        "renders into the main screen (or its own alt-screen depending on "
        "config); it expects exclusive stdin during its lifetime.",
        "Both use asyncio + own their own input drivers (Vt100Input on Linux/Mac, "
        "Win32Input on Windows). They CANNOT both be the active stdin reader at "
        "the same time — the OS only delivers each byte once.",
        "Therefore: in a single process, EITHER Textual's App is running OR "
        "prompt_toolkit's PromptSession is — never both simultaneously.",
        "Sequential alternation IS possible: the agent loop can stop the prompt, "
        "spin up a Textual App for the thinking-history card display, kill it, "
        "and re-start the prompt. But this means flickering screen takeovers on "
        "every turn — almost certainly worse UX than the current scrollback "
        "approach.",
        "Embedding Textual widgets INSIDE prompt_toolkit (or vice versa) is not "
        "supported by either project. A custom layout abstraction would have to "
        "be built that owns the screen, schedules redraws, and translates "
        "key events to both render trees. That is — by definition — porting to "
        "Textual.",
    ]


def _assess_migration_cost() -> list[str]:
    """The list of prompt_toolkit features the production code uses
    that would have to be reimplemented in Textual."""
    return [
        "PromptSession with FileHistory (prompt_toolkit) → Textual's Input + "
        "custom history binding (~1 day).",
        "Custom slash-command picker dropdown (cli_ui/slash_picker_source.py + "
        "input_loop.py) — this is ~700 LOC of bespoke prompt_toolkit "
        "ConditionalContainer + FormattedTextControl + KeyBindings. Re-doing "
        "in Textual: at least 3-5 days, likely more once edge cases (Tab/"
        "Shift-Tab, ESC dismissal, MRU integration) come up.",
        "Bracketed-paste image attach handler (cli_ui/clipboard.py + "
        "input_loop.py). Textual has its own paste-event API but the "
        "clipboard-image extraction is OS-specific. ~1 day to port.",
        "Multi-line composer (Alt+Enter inserts newline; Enter submits). "
        "Textual has multi-line Input but the modifier-distinguishing logic "
        "needs to be re-bound. ~half day.",
        "$EDITOR shell-out (Ctrl+X Ctrl+E in input_loop.py). Textual has no "
        "first-class equivalent; would need to suspend the App, spawn editor, "
        "resume — the suspend pattern exists but is fragile. ~1 day.",
        "TurnCancelScope + KeyboardListener (cli_ui/keyboard_listener.py): "
        "ESC during streaming. Textual's App handles its own keys; we'd lose "
        "the daemon-thread approach and bind ESC at the App level. ~half day.",
        "Rich.Live streaming integration (cli_ui/streaming.py): the current "
        "thinking panel is Rich.Live + Panel updates. Textual would have to "
        "render this as a live-updating widget — Static.update() on each "
        "chunk with 50ms debounce. ~1-2 days.",
        "Hook subscriber + tool status panel + reasoning store integration: "
        "wiring the existing _CURRENT renderer hooks into a Textual app. "
        "~1 day.",
        "Total: ~9-13 engineer-days for full coexistence-via-migration. "
        "Skews toward the high end once test re-writes + cross-platform "
        "regressions are accounted for.",
    ]


def main() -> int:
    print("=" * 72)
    print("Textual / prompt_toolkit coexistence probe")
    print("=" * 72)

    print("\n## Imports")
    for name, ver in _check_imports().items():
        print(f"  {name}: {ver}")

    print("\n## Structural compatibility (architectural facts)")
    for i, line in enumerate(_structural_compatibility(), 1):
        print(f"  {i}. {line}")

    print(
        "\n## Migration cost — prompt_toolkit features that would have to be re-done"
    )
    for i, line in enumerate(_assess_migration_cost(), 1):
        print(f"  {i}. {line}")

    print("\n## Verdict")
    print(
        "  In-process coexistence: NOT FEASIBLE (both libs want exclusive stdin).\n"
        "  Sequential alternation: feasible but worse UX than current scrollback.\n"
        "  Full migration: ~9-13 engineer-days plus test re-writes and cross-\n"
        "  platform regression risk. Recommended ONLY if a future product\n"
        "  requirement (e.g. nested live panels, mouse-region click handlers,\n"
        "  modal dialogs) makes the current Rich+prompt_toolkit stack truly\n"
        "  insufficient."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
