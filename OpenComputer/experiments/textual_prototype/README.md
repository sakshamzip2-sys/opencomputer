# Textual Prototype — In-Place Expand/Collapse

> **Status:** feasibility probe. Not production code. Not on the production import path. If we ever ship in-place expand/collapse, the implementation will live in `opencomputer/cli_ui/` and this directory should be deleted.

## Question being answered

Today's thinking-history card (PR #395, v6) is rendered with `console.print(Panel(...))` — once printed it's part of scrollback and can't be toggled in place. Users have asked for true Claude.ai-style click-to-expand. This prototype answers:

1. Can a Textual widget do the toggle in a way that's substantively better than what Rich + scrollback already gives us?
2. Can it coexist with our existing `prompt_toolkit` input layer in the same process?

## How to run

```bash
cd OpenComputer/experiments/textual_prototype
pip install -r requirements.txt
python -m experiments.textual_prototype.app          # standalone widget demo (interactive)
pytest test_widget.py -v                              # smoke test (3 assertions)
python -m experiments.textual_prototype.coexistence_probe   # prints findings block
```

## What this prototype proves (and what it doesn't)

**Proves:**
- A Textual widget CAN render a thinking-history card and toggle expanded/collapsed in place on a keypress, without re-printing as scrollback. Verified by `test_widget.py` (3 tests pass under `App.run_test()` + `Pilot`).

**Does NOT prove:**
- That the rendering matches v6 production styling pixel-for-pixel. The widget uses minimal CSS (`border: round $primary`); the production card uses Rich's `Panel(box=ROUNDED, ...)` with specific colors and padding. Bringing them to parity is a separate ~half-day task once a path is chosen.
- That Textual + prompt_toolkit can coexist in the same process. They cannot — see the coexistence findings below. The widget works only inside a Textual `App`.

## CI reachability

`textual` is intentionally NOT in `pyproject.toml`. CI does NOT install it. Therefore `test_widget.py` SKIPS in CI (via `pytest.importorskip`). Locally, with textual installed in the venv, the 3 tests run and pass.

**CI green ≠ prototype works** — manual local verification is the only proof.

## Findings

### What works

- The `CollapsibleThinkingCard` widget toggles between `collapsed` and `expanded` on Enter, in place, without re-printing as scrollback. Verified by `test_widget.py` (3 tests pass under `App.run_test()` + `Pilot`).
- The standalone `app.py` runs in a real terminal (`python -m experiments.textual_prototype.app`). Manual verification: pressing Enter expands; pressing Enter again collapses. Footer shows the binding hint. No flicker; redraw is sub-frame.
- Textual's reactive system + `Vertical` container handles the layout shift cleanly — the tree appears under the summary on expand, disappears on collapse, with no scrollback pollution.

### What this prototype does NOT prove

- **Pixel parity with v6 production card.** The widget uses minimal `border: round $primary` CSS; production uses Rich `Panel(box=ROUNDED, ...)` with specific colors and padding. Bringing the styling to parity is a separate ~half-day task once a path is chosen.
- **In-process integration with the existing chat loop.** The widget runs inside its own Textual `App` only. Mixing it with the existing `prompt_toolkit` PromptSession is structurally impossible (see below).

### What does NOT work — coexistence with prompt_toolkit

In-process coexistence is **not feasible**. Both libraries own stdin and the screen exclusively for their App / PromptSession lifetime. The OS only delivers each byte to one reader; whichever installed its raw-mode handler last wins.

The `coexistence_probe.py` script enumerates the architectural facts:

```
1. Textual.App.run() takes over the terminal screen via ANSI alt-screen + raw mode; it owns stdin/stdout for its lifetime.
2. prompt_toolkit.PromptSession.prompt_async() also takes over stdin and renders into the main screen (or its own alt-screen depending on config); it expects exclusive stdin during its lifetime.
3. Both use asyncio + own their own input drivers (Vt100Input on Linux/Mac, Win32Input on Windows). They CANNOT both be the active stdin reader at the same time — the OS only delivers each byte once.
4. Therefore: in a single process, EITHER Textual's App is running OR prompt_toolkit's PromptSession is — never both simultaneously.
5. Sequential alternation IS possible: the agent loop can stop the prompt, spin up a Textual App for the thinking-history card display, kill it, and re-start the prompt. But this means flickering screen takeovers on every turn — almost certainly worse UX than the current scrollback approach.
6. Embedding Textual widgets INSIDE prompt_toolkit (or vice versa) is not supported by either project. A custom layout abstraction would have to be built that owns the screen, schedules redraws, and translates key events to both render trees. That is — by definition — porting to Textual.
```

**Why this isn't empirically tested**: starting both `Textual.App.run()` and `prompt_toolkit.PromptSession.prompt_async()` in the same process races on the terminal's raw-mode setup; the second to start observes corrupted termios state and hangs or crashes the process. Running such a probe would terminate it. Both libraries' OWN documentation states they require exclusive control of stdin and the screen for the App / PromptSession lifetime — that's the authoritative source.

### Migration cost (if we decide to go all-in on Textual)

The full list of `prompt_toolkit` features the production code currently uses, with rough re-implementation estimates:

| Feature | Files | Estimate |
|---|---|---|
| FileHistory + PromptSession | `cli_ui/input_loop.py` | ~1 day |
| Slash-command picker dropdown | `cli_ui/slash_picker_source.py`, `slash_completer.py`, `slash_mru.py`, `input_loop.py` | **3–5 days** (the big one — bespoke ConditionalContainer + FormattedTextControl + KeyBindings) |
| Bracketed-paste image attach | `cli_ui/clipboard.py`, `input_loop.py` | ~1 day |
| Multi-line composer (Alt+Enter / Enter) | `cli_ui/input_loop.py` | ~0.5 day |
| `$EDITOR` shell-out (Ctrl+X Ctrl+E) | `cli_ui/input_loop.py` | ~1 day |
| TurnCancelScope + KeyboardListener (ESC during streaming) | `cli_ui/keyboard_listener.py`, `cli_ui/turn_cancel.py` | ~0.5 day |
| Rich.Live streaming integration | `cli_ui/streaming.py` | ~1–2 days |
| Hook subscriber + tool-status panel + reasoning store integration | `cli_ui/streaming.py`, `cli.py` | ~1 day |
| Cross-platform regressions + test rewrites | (whole `tests/` directory) | ~2–3 days |
| **Total** | | **9–13 engineer-days** |

### Recommended path

**Accept the limitation.** The current v6 card (PR #395) renders correctly and reads cleanly. The "true click-to-toggle" UX is desirable but not load-bearing for any user workflow we've identified. The migration cost (9–13 days, a multi-week soak risk because the slash picker is the most-touched UI surface) is disproportionate to the win.

If a future requirement makes Textual genuinely necessary — e.g. nested live panels, mouse-region click handlers, modal dialogs that block the agent loop — revisit this prototype as the proof that the widget primitive works. Until then, leave the current `console.print(Panel(...))` approach in place.

### Sequential-alternation alternative (if we MUST have toggle)

Lighter-weight middle ground: when the user presses a hotkey on the empty prompt (e.g. `Ctrl+R`), shell out to a small Textual app that renders the last N reasoning turns with toggleable cards, then quits and returns to the prompt. The flicker happens once on demand, not on every turn. ~1.5 engineer-days; preserves the existing UI for the hot path and only invokes Textual when explicitly asked.

This is the recommendation IF the user decides toggle UX is worth implementing. Otherwise, accept-the-limitation is the right call.
