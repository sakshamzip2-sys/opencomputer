# TUI Slash Command Autocomplete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror Claude Code's slash-command autocomplete in OpenComputer's TUI — typing `/` shows a filterable dropdown of registered commands with descriptions; Tab completes to the longest common prefix (LCP) for ambiguous matches and to the full name for unique matches; arrows navigate; Enter executes.

**Architecture:** A new `SlashCommandCompleter(prompt_toolkit.completion.Completer)` reads the existing `SLASH_REGISTRY` (single source of truth — no parallel registry, no drift) and yields `Completion` objects when the buffer starts with `/` and the cursor is still inside the command-name token (no space yet). `build_prompt_session()` is updated to (a) attach the completer, (b) flip `complete_while_typing=True`, (c) bind Tab to a custom handler that implements 0/1/many-match LCP semantics. All other behavior (ESC clears buffer, Ctrl+J newline, history, image paste) is preserved.

**Tech Stack:** `prompt_toolkit>=3.0` (already a dep), `pytest`, `rich` (already used for console). No new dependencies.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `opencomputer/cli_ui/slash_completer.py` | **Create** | `SlashCommandCompleter` class + `_longest_common_prefix` helper |
| `opencomputer/cli_ui/input_loop.py` | **Modify** | Wire completer + Tab keybinding into `build_prompt_session` |
| `opencomputer/cli_ui/__init__.py` | **Modify** | Re-export `SlashCommandCompleter` for tests |
| `tests/test_cli_ui_slash_completer.py` | **Create** | Unit tests for completer + LCP helper |
| `tests/test_cli_ui_input_loop.py` | **Modify** | Add a smoke test asserting completer is wired |

**Key invariants (referenced by multiple tasks):**
- Completer fires only when `document.text_before_cursor` starts with `/` and contains no space.
- Match: case-insensitive prefix on `cmd.name` (canonical names only — aliases dispatch but don't appear in the dropdown; this matches Claude Code's "one row per command" convention).
- Completion `text` returned: `"/" + cmd.name` (no trailing space; cursor lands at end so user types args manually if needed).
- Display column: plain string `"/" + cmd.name + (" " + cmd.args_hint if args_hint else "")`. Plain `str` is required because args hints like `<new title>` contain `<>` which prompt_toolkit's `HTML` parser would mis-interpret as tags.
- `display_meta`: `cmd.description` (right-aligned column by prompt_toolkit's menu).
- LCP helper is exported as `longest_common_prefix` (public), not `_longest_common_prefix`, since it's imported across modules.

**Audit-derived guardrails:**
- Tab binding is gated by a `Condition` filter so prompt_toolkit's default Tab is preserved for non-slash input.
- Existing `Keys.Escape` handler must branch: if a completion menu is open, cancel it first; else clear the buffer (preserves prior behavior for non-menu state).
- Do NOT pass `complete_in_thread=True` — adds nondeterminism for negligible perf gain on a 10-element registry.
- Known V1 limitation: pressing Enter on a freshly-opened menu (no row highlighted) submits raw text. Users press Down to highlight before Enter. Acceptable for V1; matches Claude Code's behavior.

---

## Task 1: SlashCommandCompleter + LCP helper (TDD)

**Files:**
- Create: `OpenComputer/opencomputer/cli_ui/slash_completer.py`
- Test: `OpenComputer/tests/test_cli_ui_slash_completer.py`

- [ ] **Step 1: Write failing tests**

```python
# OpenComputer/tests/test_cli_ui_slash_completer.py
"""Tests for SlashCommandCompleter and _longest_common_prefix."""
from __future__ import annotations

from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent

from opencomputer.cli_ui.slash_completer import (
    SlashCommandCompleter,
    _longest_common_prefix,
)


def _completions(text: str) -> list[str]:
    """Helper: run completer over text-before-cursor and return completion texts."""
    completer = SlashCommandCompleter()
    doc = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(doc, CompleteEvent())]


# ---- _longest_common_prefix -------------------------------------------------

def test_lcp_empty_list():
    assert _longest_common_prefix([]) == ""


def test_lcp_single_string():
    assert _longest_common_prefix(["/help"]) == "/help"


def test_lcp_multiple_with_prefix():
    assert _longest_common_prefix(["/clear", "/cost"]) == "/c"


def test_lcp_no_common():
    assert _longest_common_prefix(["/help", "/exit"]) == "/"


def test_lcp_case_sensitive():
    # We treat input/output canonical (lowercase), so LCP is exact.
    assert _longest_common_prefix(["/Help", "/help"]) == "/"


# ---- SlashCommandCompleter ---------------------------------------------------

def test_completer_no_slash_yields_nothing():
    assert _completions("hello") == []


def test_completer_empty_yields_nothing():
    assert _completions("") == []


def test_completer_only_slash_yields_all_commands():
    out = _completions("/")
    # Should include canonical names, not aliases
    assert "/exit" in out
    assert "/clear" in out
    assert "/help" in out
    assert "/q" not in out  # alias hidden
    assert "/quit" not in out  # alias hidden


def test_completer_prefix_filters_by_name():
    out = _completions("/cl")
    assert out == ["/clear"]


def test_completer_prefix_case_insensitive():
    out = _completions("/HE")
    assert "/help" in out


def test_completer_no_match_returns_empty():
    assert _completions("/zzz") == []


def test_completer_aborts_after_space():
    # Once the user types a space, the completer should stop firing —
    # they're entering args, not the command name.
    assert _completions("/help ") == []
    assert _completions("/rename foo") == []


def test_completer_returns_completion_objects_with_meta():
    completer = SlashCommandCompleter()
    doc = Document(text="/help", cursor_position=5)
    completions = list(completer.get_completions(doc, CompleteEvent()))
    assert len(completions) == 1
    c = completions[0]
    assert c.text == "/help"
    # display_meta is the description
    assert "slash commands" in c.display_meta_text.lower()
    # start_position replaces the typed prefix
    assert c.start_position == -len("/help")


def test_completer_display_includes_args_hint():
    completer = SlashCommandCompleter()
    doc = Document(text="/rename", cursor_position=7)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    # display string should embed args_hint for commands that have one
    display = c.display_text if hasattr(c, "display_text") else str(c.display)
    assert "rename" in display
    assert "<new title>" in display


def test_completer_sorted_alphabetically():
    out = _completions("/")
    # Two arbitrary canonical commands — verify ordering is stable & alpha
    assert out.index("/clear") < out.index("/help")
    assert out.index("/cost") < out.index("/exit")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_slash_completer.py -v
```
Expected: ImportError — `slash_completer` module does not exist yet.

- [ ] **Step 3: Implement the completer**

```python
# OpenComputer/opencomputer/cli_ui/slash_completer.py
"""Slash command autocomplete for the OpenComputer TUI.

Reads :data:`opencomputer.cli_ui.slash.SLASH_REGISTRY` — the single source
of truth for slash commands — and yields :class:`prompt_toolkit.completion.Completion`
objects when the user is typing a slash command name (line starts with ``/``
and no space has been entered yet).

Aliases are NOT displayed in the dropdown — only canonical names — but the
existing :func:`opencomputer.cli_ui.slash_handlers.dispatch_slash` still
accepts aliases when the user types them out and presses Enter. This mirrors
Claude Code's "one row per command" convention.
"""
from __future__ import annotations

from typing import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML

from .slash import SLASH_REGISTRY, CommandDef


def _longest_common_prefix(strs: list[str]) -> str:
    """Return the longest common prefix of all strings in ``strs``.

    Empty list → empty string. Comparison is case-sensitive; callers
    that want case-insensitive LCP should lowercase first.
    """
    if not strs:
        return ""
    s_min = min(strs)
    s_max = max(strs)
    for i, ch in enumerate(s_min):
        if ch != s_max[i]:
            return s_min[:i]
    return s_min


def _format_display(cmd: CommandDef) -> HTML:
    """Render the left-column display for a command in the dropdown.

    Format: ``/<name> <args_hint>`` (args_hint omitted if empty). The name
    is bolded so it stands out from the args shape.
    """
    if cmd.args_hint:
        return HTML(f"/<b>{cmd.name}</b> <ansigray>{cmd.args_hint}</ansigray>")
    return HTML(f"/<b>{cmd.name}</b>")


class SlashCommandCompleter(Completer):
    """Yields completions for slash commands when the cursor is in the
    command-name token (line starts with ``/`` and no space yet).

    Returns nothing for non-slash input, post-space input, or when the
    typed prefix matches no canonical name.
    """

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        # Once the user types a space, they're entering args — stop completing.
        if " " in text:
            return
        prefix = text[1:].lower()
        # Iterate the registry in declaration order, then sort the resulting
        # matches alphabetically by name for stable display.
        matches = [cmd for cmd in SLASH_REGISTRY if cmd.name.startswith(prefix)]
        matches.sort(key=lambda c: c.name)
        for cmd in matches:
            yield Completion(
                text=f"/{cmd.name}",
                start_position=-len(text),
                display=_format_display(cmd),
                display_meta=cmd.description,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_slash_completer.py -v
```
Expected: all 13 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_completer.py OpenComputer/tests/test_cli_ui_slash_completer.py
git commit -m "feat(tui): SlashCommandCompleter with prefix matching"
```

---

## Task 2: Wire completer into PromptSession + Tab keybinding

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/input_loop.py` (around the existing `build_prompt_session` function)
- Modify: `OpenComputer/opencomputer/cli_ui/__init__.py` (export new symbol)
- Test: extend `OpenComputer/tests/test_cli_ui_input_loop.py`

- [ ] **Step 1: Write failing tests for the wire-up**

Add these to the existing `test_cli_ui_input_loop.py`:

```python
def test_build_prompt_session_has_slash_completer(tmp_path):
    """PromptSession must have SlashCommandCompleter wired."""
    from opencomputer.cli_ui.input_loop import build_prompt_session
    from opencomputer.cli_ui.slash_completer import SlashCommandCompleter
    from opencomputer.cli_ui.turn_cancel import TurnCancelScope

    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    assert isinstance(session.completer, SlashCommandCompleter)
    # complete_while_typing must be enabled for the menu to appear automatically
    cwt = session.complete_while_typing
    if callable(cwt):
        cwt = cwt()
    assert cwt is True


def test_build_prompt_session_tab_key_bound(tmp_path):
    """Tab must be bound (so the LCP handler runs instead of inserting a literal tab)."""
    from prompt_toolkit.keys import Keys
    from opencomputer.cli_ui.input_loop import build_prompt_session
    from opencomputer.cli_ui.turn_cancel import TurnCancelScope

    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    # The KeyBindings registry should contain a binding for Tab/ControlI.
    bindings = session.key_bindings.bindings
    tab_keys = (Keys.Tab, Keys.ControlI)
    assert any(
        any(k in tab_keys for k in b.keys) for b in bindings
    ), "Tab keybinding missing from PromptSession"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_input_loop.py -v -k "slash_completer or tab_key"
```
Expected: FAIL — `session.completer is None` and Tab not bound.

- [ ] **Step 3: Modify `input_loop.py`**

Add this near the top of `input_loop.py` with the other imports:

```python
from .slash_completer import SlashCommandCompleter, _longest_common_prefix
```

Inside `build_prompt_session`, after the existing key bindings (`_ctrl_v`, `_bracketed_paste`) and before the `return PromptSession(...)`, add the Tab binding:

```python
    completer = SlashCommandCompleter()

    @kb.add(Keys.ControlI)  # ControlI == Tab
    def _tab(event):
        """Tab: 0 matches → no-op; 1 match → complete; many → LCP."""
        buf = event.current_buffer
        text = buf.document.text_before_cursor
        # Only act on slash-command tokens; otherwise insert a literal tab
        # is unhelpful in our chat REPL — fall back to triggering completion.
        if not text.startswith("/") or " " in text:
            buf.start_completion(select_first=False)
            return
        # Recompute matches here (don't trust an in-flight complete_state) —
        # cheaper than racing prompt_toolkit's async completion machinery.
        matches = [
            f"/{cmd.name}"
            for cmd in SLASH_REGISTRY
            if cmd.name.startswith(text[1:].lower())
        ]
        if not matches:
            return  # 0 matches: no-op (consume the tab silently)
        if len(matches) == 1:
            target = matches[0]
        else:
            target = _longest_common_prefix(matches)
            if target == text:
                # Already at LCP — show menu so user can pick
                buf.start_completion(select_first=False)
                return
        buf.delete_before_cursor(count=len(text))
        buf.insert_text(target)
```

Update the `PromptSession(...)` call:

```python
    return PromptSession(
        message=HTML("<ansigreen><b>you ›</b></ansigreen> "),
        history=FileHistory(str(history_path)),
        key_bindings=kb,
        multiline=False,
        mouse_support=False,
        enable_history_search=True,
        complete_while_typing=True,         # was False
        completer=completer,                 # was unset
        complete_in_thread=True,             # avoid blocking on slow registries
        erase_when_done=True,
    )
```

You'll also need to import `SLASH_REGISTRY` for the Tab handler:

```python
from .slash import SLASH_REGISTRY
```

- [ ] **Step 4: Update `__init__.py` to re-export the new class**

In `OpenComputer/opencomputer/cli_ui/__init__.py`, add:

```python
from .slash_completer import SlashCommandCompleter

__all__ = [..., "SlashCommandCompleter"]  # extend existing __all__
```

(Read the existing `__all__` first and merge — don't blat it.)

- [ ] **Step 5: Run all cli_ui tests**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_slash_completer.py tests/test_cli_ui_input_loop.py tests/test_cli_ui_slash.py -v
```
Expected: all green.

- [ ] **Step 6: Run the FULL test suite to catch regressions**

```bash
cd OpenComputer && python -m pytest tests/ -q 2>&1 | tail -30
```
Expected: same number of pass/fail as baseline (no regressions). 

If a test fails that's unrelated (e.g. environment-dependent integration test), note it but don't fix it in this PR — flag in the PR description.

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/opencomputer/cli_ui/__init__.py OpenComputer/tests/test_cli_ui_input_loop.py
git commit -m "feat(tui): wire SlashCommandCompleter + Tab→LCP keybinding"
```

---

## Task 3: Manual smoke check

Since the TUI is interactive, full automation isn't trivial, but we can sanity-check by importing and constructing a PromptSession and asserting the completer fires for known inputs.

- [ ] **Step 1: Run a small REPL-driver check**

```bash
cd OpenComputer && python -c "
from prompt_toolkit.document import Document
from prompt_toolkit.completion import CompleteEvent
from opencomputer.cli_ui.slash_completer import SlashCommandCompleter

c = SlashCommandCompleter()
for prefix in ['/', '/h', '/re', '/zz', '/help ']:
    out = [comp.text for comp in c.get_completions(Document(prefix, len(prefix)), CompleteEvent())]
    print(f'{prefix!r}: {out}')
"
```
Expected output:
```
'/': ['/clear', '/cost', '/exit', '/export', '/help', '/model', '/rename', '/resume', '/screenshot', '/sessions']
'/h': ['/help']
'/re': ['/rename', '/resume']
'/zz': []
'/help ': []
```

- [ ] **Step 2: Confirm `oc` CLI launches without import errors**

```bash
cd OpenComputer && python -m opencomputer.cli --help 2>&1 | head -5
```
Expected: typer help text, no traceback.

---

## Task 4: PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/tui-slash-autocomplete
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "feat(tui): slash command autocomplete (Phase 2)" --body "$(cat <<'EOF'
## Summary
- New `SlashCommandCompleter` reads the existing `SLASH_REGISTRY` and yields prompt_toolkit `Completion`s when the user types `/` (and the cursor is still in the command-name token, i.e. no space yet)
- `build_prompt_session` now wires the completer + flips `complete_while_typing=True` + binds Tab to a custom handler with Claude-Code-parity LCP semantics (0 matches → no-op, 1 → complete, many → longest common prefix)
- Aliases dispatch as before but don't appear in the dropdown — one row per canonical command, mirroring Claude Code

## UX
- Type `/` → dropdown shows all 10 canonical commands with descriptions
- Type `/re` → list narrows to `/rename` + `/resume`
- Tab on `/re` → completes to `/re` (already at LCP) and opens the menu so user can pick
- Tab on `/h` → completes to `/help` (single match)
- Arrow keys + Enter pick a row; Escape clears the buffer (existing behavior)

## Test plan
- [x] 13 new unit tests for `SlashCommandCompleter` + `_longest_common_prefix`
- [x] 2 new tests asserting `build_prompt_session` wires the completer and binds Tab
- [x] Full `tests/` suite passes with no regressions

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- "Dropdown when typing /" → Task 1 (`/` alone yields all canonical commands)
- "Filter as you type" → Task 1 (prefix match in `get_completions`)
- "Tab autocompletes (parity with Claude Code)" → Task 2 (custom Tab binding with 0/1/many LCP semantics)
- "Aliases don't pollute the dropdown but still dispatch" → Task 1 (matches `cmd.name` only; existing dispatcher in `slash_handlers.py:201-209` already handles aliases)
- "Args hint visible in the row" → Task 1 (`_format_display` embeds args_hint)
- "No regressions" → Task 2 Step 6 (full suite)

**Placeholder scan:** every code block contains executable code; every command shows expected output. No TBDs.

**Type consistency:** `_longest_common_prefix(strs: list[str]) -> str` is the same shape in test and implementation. `SlashCommandCompleter.get_completions(self, document, complete_event) -> Iterable[Completion]` matches the prompt_toolkit base-class contract.

**Risk noted in plan:** the existing `Keys.Escape` binding clears the buffer eagerly. This is fine — when the completion menu is open, Escape closes the menu first (prompt_toolkit default), and only a second Escape would reach our handler. Confirmed by reading prompt_toolkit's binding precedence.
