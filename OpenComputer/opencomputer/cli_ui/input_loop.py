"""Input layer for the chat loop.

Replaces ``Console.input(...)`` with a ``prompt_toolkit.PromptSession``
that supports:

- Persistent ``FileHistory`` (Up-arrow recalls across sessions)
- ``Alt+Enter`` / ``Ctrl+J`` insert literal newline (multi-line input)
- ``Esc`` (during the prompt phase) clears the input buffer
- Bracketed paste (handled automatically by prompt_toolkit) — and an
  ADDITIONAL custom handler that detects "empty paste" as a clipboard
  image attempt, saves the image to disk, and inserts a placeholder
  token into the buffer that the chat loop unpacks into Message
  attachments.
- ``Ctrl+V`` as a fallback path for terminals that don't support the
  bracketed-paste protocol (Linux GNOME Terminal, Konsole, etc.)
- ``mouse_support=False`` (we want native terminal selection for copy)

Mid-stream ESC interrupt is NOT handled here — see
:mod:`opencomputer.cli_ui.keyboard_listener` (a daemon thread that runs
during streaming when prompt_toolkit isn't active).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.shortcuts import CompleteStyle

from opencomputer.cli_ui.clipboard import has_clipboard_image, save_clipboard_image
from opencomputer.cli_ui.slash import SLASH_REGISTRY
from opencomputer.cli_ui.slash_completer import (
    SlashCommandCompleter,
    longest_common_prefix,
)
from opencomputer.cli_ui.turn_cancel import TurnCancelScope

#: Regex that finds image-attachment placeholders in a submitted message.
#: Format: ``[image: /abs/path/to/file.png]``. Captures the path so the
#: chat loop can extract attachments and strip the marker before passing
#: text to the LLM.
IMAGE_PLACEHOLDER_RE = re.compile(r"\[image:\s*([^\]]+?)\]")


def _images_dir(profile_home: Path) -> Path:
    """``<profile_home>/images/`` — created lazily."""
    p = profile_home / "images"
    p.mkdir(parents=True, exist_ok=True)
    return p


def extract_image_attachments(text: str) -> tuple[str, list[str]]:
    """Pull image-placeholder paths out of *text*.

    Returns ``(text_without_placeholders, list_of_paths)``. Paths are
    de-duplicated while preserving first-seen order. The text with
    placeholders removed gets newline-collapsed at submit time so the
    LLM sees a clean prompt — the placeholder string itself never
    reaches the model.
    """
    paths: list[str] = []
    seen: set[str] = set()

    def _collect(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
        return ""

    cleaned = IMAGE_PLACEHOLDER_RE.sub(_collect, text)
    # Collapse runs of empty whitespace from removed placeholders.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, paths


def _history_file_path(profile_home: Path) -> Path:
    """Resolve the history file path; ensure the parent dir exists."""
    profile_home.mkdir(parents=True, exist_ok=True)
    return profile_home / "input_history"


def _strip_trailing_whitespace(text: str) -> str:
    """Strip trailing whitespace. Multi-line input keeps inner formatting."""
    return text.rstrip()


def build_prompt_session(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
) -> PromptSession:
    """Construct a fresh PromptSession bound to ``scope``.

    Build per-turn (not once at startup) so each turn gets a clean
    ``TurnCancelScope`` and the key bindings always close over the
    *current* scope, not a stale one from a previous turn.
    """
    history_path = _history_file_path(profile_home)
    kb = KeyBindings()

    @kb.add(Keys.Escape, eager=True)
    def _esc(event):  # noqa: ANN001
        # ESC during *idle* prompt: if a completion menu is open (slash
        # autocomplete dropdown), close that first — only clear the
        # buffer on a "fresh" Escape with no menu visible. Without this
        # branch, opening the menu and pressing Escape would wipe the
        # user's typed text instead of just dismissing the dropdown.
        # ESC during *streaming*: handled by KeyboardListener thread; the
        # prompt isn't the active app at that point.
        buf = event.current_buffer
        if buf.complete_state is not None:
            buf.cancel_completion()
            return
        buf.text = ""

    @kb.add(Keys.ControlJ)
    def _ctrl_j(event):  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    @kb.add(Keys.Escape, Keys.Enter)
    def _alt_enter(event):  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    # Image clipboard paste — Ctrl+V fallback for terminals without
    # bracketed-paste support, or where the OS sends raw paste through
    # cmd-v rather than bracketed sequences. Bracketed-paste handling
    # is in @kb.add(Keys.BracketedPaste, ...) below.
    @kb.add(Keys.ControlV)
    def _ctrl_v(event):  # noqa: ANN001
        _try_attach_clipboard_image_into_buffer(event, profile_home=profile_home)

    @kb.add(Keys.BracketedPaste)
    def _bracketed_paste(event):  # noqa: ANN001
        # ``data`` is the text that was bracket-pasted. If it's empty /
        # whitespace-only, treat it as a clipboard image attempt — modern
        # terminals send a zero-length paste sequence when the user pastes
        # binary clipboard data. Otherwise just insert the text verbatim.
        data: str = getattr(event, "data", "") or ""
        if not data.strip() and _try_attach_clipboard_image_into_buffer(
            event, profile_home=profile_home
        ):
            return
        event.current_buffer.insert_text(data)

    # --- Slash autocomplete: Tab → LCP semantics --------------------------
    # Active only while the user is typing the command-name token of a
    # slash command (line starts with '/' and no space yet). Outside that
    # condition the binding doesn't fire and prompt_toolkit's default Tab
    # handling applies — which, given our completer returns no completions
    # for non-slash text, is effectively a no-op. This keeps the chat REPL
    # behavior unchanged for normal messages.

    def _in_slash_token() -> bool:
        try:
            from prompt_toolkit.application.current import get_app

            txt = get_app().current_buffer.document.text_before_cursor
        except Exception:
            return False
        return txt.startswith("/") and " " not in txt

    @kb.add(Keys.ControlI, filter=Condition(_in_slash_token))
    def _tab(event):  # noqa: ANN001
        """Tab on a slash-command prefix:

        - 0 matches  → no-op (consume the keypress; don't insert a tab).
        - 1 match    → complete to ``/<name>``.
        - many       → complete to the longest common prefix; if that's
                       already what the user typed, open the menu so they
                       can pick visually.
        """
        buf = event.current_buffer
        text = buf.document.text_before_cursor
        prefix = text[1:].lower()
        matches = [
            f"/{cmd.name}" for cmd in SLASH_REGISTRY if cmd.name.startswith(prefix)
        ]
        if not matches:
            return
        if len(matches) == 1:
            target = matches[0]
        else:
            target = longest_common_prefix(matches)
            if target == text:
                buf.start_completion(select_first=False)
                return
        buf.delete_before_cursor(count=len(text))
        buf.insert_text(target)

    return PromptSession(
        message=HTML("<ansigreen><b>you ›</b></ansigreen> "),
        history=FileHistory(str(history_path)),
        key_bindings=kb,
        multiline=False,
        mouse_support=False,
        enable_history_search=True,
        # complete_while_typing=True opens the slash autocomplete dropdown
        # automatically as the user types (no need to hit Tab first). The
        # SlashCommandCompleter returns nothing for non-slash input, so
        # plain chat messages don't trigger any visible menu.
        complete_while_typing=True,
        completer=SlashCommandCompleter(),
        # MULTI_COLUMN puts the completion menu into the main layout
        # (Window-based) instead of using a Float widget that depends on
        # Cursor-Position-Report. This makes the menu visible in editor
        # terminals (VS Code, JetBrains) that don't reliably respond to
        # CPR. Trade-off: layout looks like fish/zsh tab completion
        # (commands in a grid + meta toolbar showing the highlighted
        # command's description) rather than a single-column popup with
        # descriptions on every row. Acceptable V1 — strict Claude-Code
        # parity is a follow-up requiring a custom Application layout.
        complete_style=CompleteStyle.MULTI_COLUMN,
        # erase_when_done clears the typed prompt line on submit so the
        # chat loop can re-render the user's message inside a styled
        # boundary box (no duplicate "you › ..." line in scrollback).
        erase_when_done=True,
    )


def _try_attach_clipboard_image_into_buffer(
    event,  # noqa: ANN001 — prompt_toolkit KeyPressEvent
    *,
    profile_home: Path,
) -> bool:
    """Save clipboard image to disk and insert ``[image: <path>]`` token.

    Returns True if an image was found and inserted; False otherwise so
    the caller can fall back to plain text-paste behavior.
    """
    if not has_clipboard_image():
        return False
    images_dir = _images_dir(profile_home)
    ts = time.strftime("%Y%m%d-%H%M%S")
    counter = int(time.time() * 1000) % 100000
    dest = images_dir / f"clip_{ts}_{counter}.png"
    if not save_clipboard_image(dest):
        return False
    placeholder = f"[image: {dest}]"
    # Insert with a leading space if the buffer already has text and
    # doesn't end with whitespace, so the placeholder is parseable.
    text_before = event.current_buffer.text
    if text_before and not text_before.endswith((" ", "\n", "\t")):
        placeholder = " " + placeholder
    event.current_buffer.insert_text(placeholder)
    return True


async def read_user_input(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
) -> str:
    """Read one line of user input with an always-visible slash dropdown.

    Replaces the older :func:`build_prompt_session` path because
    prompt_toolkit's built-in :class:`CompletionsMenu` (both
    ``COLUMN`` and ``MULTI_COLUMN`` styles) silently fails to render in
    editor terminals (VS Code, JetBrains) where Cursor-Position-Report
    handling is unreliable. We build a custom :class:`Application` with
    our own dropdown :class:`Window` in the main layout — pure layout
    flow, no Float widgets, no CPR dependency — guaranteed to render.

    UX:

    - Type ``/`` → dropdown shows all 10 canonical slash commands with
      ``(category)`` tag and description on each row
    - Type ``/re`` → list narrows to commands starting with that prefix
    - Up/Down arrow keys navigate the dropdown (highlighted row in bold
      with a blue background)
    - Tab → autocomplete to the highlighted command name
    - Enter → if dropdown is open and a row is highlighted, expand to
      that command name then submit; otherwise submit raw text
    - Esc → dismiss the dropdown if open; clear buffer otherwise
    - Ctrl+J / Alt+Enter → insert literal newline
    - Ctrl+V / bracketed paste → handles clipboard images (existing flow)
    - Ctrl+C / Ctrl+D (empty buffer) → raise to caller per shell convention

    ``build_prompt_session`` is preserved as the legacy entry point used
    by older callers and several test fixtures; new code should use this.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.layout import (
        ConditionalContainer,
        HSplit,
        Layout,
        VSplit,
        Window,
    )
    from prompt_toolkit.layout.controls import (
        BufferControl,
        FormattedTextControl,
    )
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.styles import Style

    from .slash import SLASH_REGISTRY

    history_path = _history_file_path(profile_home)
    history = FileHistory(str(history_path))

    input_buffer = Buffer(
        history=history,
        multiline=False,
        complete_while_typing=False,
        enable_history_search=True,
    )

    # Mutable picker state. Updated on every keystroke via on_text_changed
    # and consumed by the dropdown FormattedTextControl on each render.
    state: dict = {"matches": [], "selected_idx": 0}

    def _refilter(text: str) -> None:
        if text.startswith("/") and " " not in text:
            prefix = text[1:].lower()
            state["matches"] = [
                c for c in SLASH_REGISTRY if c.name.startswith(prefix)
            ][:10]
            state["selected_idx"] = 0
        else:
            state["matches"] = []
            state["selected_idx"] = 0

    def _on_text_changed(_buf):  # noqa: ANN001 — pt fires (sender,)
        _refilter(input_buffer.text)

    input_buffer.on_text_changed += _on_text_changed

    def _has_dropdown() -> bool:
        return bool(state["matches"])

    def _dropdown_text():
        if not state["matches"]:
            return []
        out: list[tuple[str, str]] = []
        for i, cmd in enumerate(state["matches"]):
            is_sel = i == state["selected_idx"]
            arrow = "❯ " if is_sel else "  "
            args = f" {cmd.args_hint}" if cmd.args_hint else ""
            cmd_str = f"{arrow}/{cmd.name}{args}"
            cat_str = f"  ({cmd.category})"
            desc_str = f"  {cmd.description}"
            base = "class:dd.selected" if is_sel else "class:dd"
            cat_cls = "class:dd.cat.selected" if is_sel else "class:dd.cat"
            desc_cls = "class:dd.desc.selected" if is_sel else "class:dd.desc"
            out.append((base, cmd_str))
            out.append((cat_cls, cat_str))
            out.append((desc_cls, desc_str))
            out.append((base, "\n"))
        return out

    def _dropdown_height():
        return Dimension(exact=min(len(state["matches"]), 10))

    kb = KeyBindings()

    @kb.add(Keys.Up, filter=Condition(_has_dropdown))
    def _up(event):  # noqa: ANN001
        if state["matches"]:
            state["selected_idx"] = max(0, state["selected_idx"] - 1)

    @kb.add(Keys.Down, filter=Condition(_has_dropdown))
    def _down(event):  # noqa: ANN001
        if state["matches"]:
            state["selected_idx"] = min(
                len(state["matches"]) - 1, state["selected_idx"] + 1
            )

    @kb.add(Keys.ControlI, filter=Condition(_has_dropdown))  # Tab
    def _tab(event):  # noqa: ANN001
        sel = state["matches"][state["selected_idx"]]
        input_buffer.text = f"/{sel.name}"
        input_buffer.cursor_position = len(input_buffer.text)

    @kb.add(Keys.Enter)
    def _enter(event):  # noqa: ANN001
        # If dropdown is open and a row is selected, expand to that
        # command before submitting (so the row visibly chosen wins).
        if state["matches"] and 0 <= state["selected_idx"] < len(state["matches"]):
            sel = state["matches"][state["selected_idx"]]
            input_buffer.text = f"/{sel.name}"
        event.app.exit(result=input_buffer.text)

    @kb.add(Keys.Escape, eager=True)
    def _esc(event):  # noqa: ANN001
        # ESC dismisses the dropdown if open; otherwise clears the buffer
        # (matches the prior PromptSession behavior).
        if state["matches"]:
            state["matches"] = []
            state["selected_idx"] = 0
        else:
            input_buffer.text = ""

    @kb.add(Keys.ControlC)
    def _ctrl_c(event):  # noqa: ANN001
        event.app.exit(exception=KeyboardInterrupt)

    @kb.add(Keys.ControlD)
    def _ctrl_d(event):  # noqa: ANN001
        if not input_buffer.text:
            event.app.exit(exception=EOFError)

    @kb.add(Keys.ControlJ)
    def _ctrl_j(event):  # noqa: ANN001
        input_buffer.insert_text("\n")

    @kb.add(Keys.Escape, Keys.Enter)
    def _alt_enter(event):  # noqa: ANN001
        input_buffer.insert_text("\n")

    @kb.add(Keys.ControlV)
    def _ctrl_v(event):  # noqa: ANN001
        _try_attach_clipboard_image_into_buffer(event, profile_home=profile_home)

    @kb.add(Keys.BracketedPaste)
    def _bracketed_paste(event):  # noqa: ANN001
        data: str = getattr(event, "data", "") or ""
        if not data.strip() and _try_attach_clipboard_image_into_buffer(
            event, profile_home=profile_home
        ):
            return
        event.current_buffer.insert_text(data)

    style = Style.from_dict(
        {
            "prompt": "ansigreen bold",
            "dd": "#a8a8a8",
            "dd.selected": "bold #ffffff bg:#005f87",
            "dd.cat": "#5fafd7",
            "dd.cat.selected": "#bcbcbc bg:#005f87",
            "dd.desc": "#5fd75f",
            "dd.desc.selected": "#5fd75f bg:#005f87 bold",
        }
    )

    prompt_window = Window(
        content=FormattedTextControl([("class:prompt", "you › ")]),
        height=1,
        dont_extend_width=True,
    )
    input_window = Window(
        content=BufferControl(buffer=input_buffer),
        height=1,
    )
    dropdown_window = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(_dropdown_text),
            height=_dropdown_height,
            wrap_lines=False,
        ),
        filter=Condition(_has_dropdown),
    )

    layout = Layout(
        HSplit(
            [
                VSplit([prompt_window, input_window]),
                dropdown_window,
            ]
        ),
        focused_element=input_window,
    )

    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )

    text = await app.run_async()
    return _strip_trailing_whitespace(text or "")
