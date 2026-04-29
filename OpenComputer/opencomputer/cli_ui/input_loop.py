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
from opencomputer.cli_ui.paste_folder import PasteFolder
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


def _render_dropdown_for_state(state: dict) -> list[tuple[str, str]]:
    """Render a dropdown row list from the picker state dict.

    Pulled out of ``read_user_input`` (was a closure) so unit tests can
    exercise the rendering logic without spinning up an Application.

    Returns a list of (style-class, text) pairs suitable for
    :class:`FormattedTextControl`.
    """
    from .slash import CommandDef, SkillEntry
    from .slash_completer import _trim_description

    matches = state.get("matches") or []
    if not matches:
        return []
    out: list[tuple[str, str]] = []
    if state.get("mode") == "file":
        # File-completion rendering — unchanged.
        from opencomputer.cli_ui.file_completer import format_size_label

        for i, p in enumerate(matches):
            is_sel = i == state["selected_idx"]
            cursor_cls = "class:dd.cursor" if is_sel else "class:dd.cursor.dim"
            title_cls = "class:dd.title.selected" if is_sel else "class:dd.title"
            desc_cls = "class:dd.desc.selected" if is_sel else "class:dd.desc"
            size = format_size_label(p, base=Path.cwd())
            out.append((cursor_cls, "❯ " if is_sel else "  "))
            out.append((title_cls, f"@{p}"))
            if size:
                out.append((desc_cls, f"  ({size})"))
            out.append(("", "\n"))
        return out
    # Slash command + skill rendering — handles both SlashItem variants.
    for i, item in enumerate(matches):
        is_sel = i == state["selected_idx"]
        cursor_cls = "class:dd.cursor" if is_sel else "class:dd.cursor.dim"
        title_cls = "class:dd.title.selected" if is_sel else "class:dd.title"
        desc_cls = "class:dd.desc.selected" if is_sel else "class:dd.desc"
        if isinstance(item, CommandDef):
            args = f" {item.args_hint}" if item.args_hint else ""
            label = f"/{item.name}{args}"
            tag = "(command)"
            desc = item.description
            cat_cls = (
                "class:dd.cat.selected" if is_sel else "class:dd.tag.command"
            )
        elif isinstance(item, SkillEntry):
            label = f"/{item.id}"
            tag = "(skill)"
            desc = item.description
            cat_cls = (
                "class:dd.cat.selected" if is_sel else "class:dd.tag.skill"
            )
        else:
            # Unknown item kind — skip rather than render garbage.
            continue
        out.append((cursor_cls, "❯ " if is_sel else "  "))
        out.append((title_cls, label))
        out.append((cat_cls, f"  {tag}"))
        out.append((desc_cls, f"  {_trim_description(desc)}"))
        out.append(("", "\n"))
    return out


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

    # Tier 2.B — Ctrl+X Ctrl+E opens the current buffer in $EDITOR
    # (bash convention). Useful for composing long prompts with full
    # editor affordances (vim/nvim/code/etc.) before sending.
    # prompt_toolkit's Buffer.open_in_editor handles the spawn + read-back.
    @kb.add(Keys.ControlX, Keys.ControlE)
    def _ctrl_x_ctrl_e(event):  # noqa: ANN001
        try:
            event.current_buffer.open_in_editor()
        except Exception:  # noqa: BLE001
            # Editor missing or spawn failed — keep buffer intact rather
            # than crashing the prompt session.
            pass

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
    session_title: str | None = None,
    paste_folder: PasteFolder | None = None,
    memory_manager: object | None = None,
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

    ``memory_manager``: optional MemoryManager for sourcing skills into
    the dropdown. When provided, the picker mixes commands and skills
    via UnifiedSlashSource. When ``None``, only built-in commands appear
    (legacy fallback for callers that haven't been updated yet).

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
    from .slash_mru import MruStore
    from .slash_picker_source import UnifiedSlashSource

    history_path = _history_file_path(profile_home)
    history = FileHistory(str(history_path))

    # MRU store + picker source — lifted out of the legacy SLASH_REGISTRY
    # path so skills surface in the dropdown alongside commands. When
    # ``memory_manager`` is None (legacy callers), picker_source stays
    # None and _refilter falls back to startswith-on-commands.
    mru_store = MruStore(profile_home / "slash_mru.json")
    picker_source: UnifiedSlashSource | None = (
        UnifiedSlashSource(memory_manager, mru_store)
        if memory_manager is not None
        else None
    )

    input_buffer = Buffer(
        history=history,
        multiline=False,
        complete_while_typing=False,
        enable_history_search=True,
    )

    # Mutable picker state. Updated on every keystroke via on_text_changed
    # and consumed by the dropdown FormattedTextControl on each render.
    # ``mode`` is "slash" | "file" | "" — distinguishes which dropdown
    # template to render and where to insert on Tab/Enter.
    state: dict = {
        "matches": [],
        "selected_idx": 0,
        "mode": "",
        "at_token_range": None,  # (start, end) when mode == "file"
    }

    def _refilter(text: str) -> None:
        # Slash prefix wins (commands + skills via picker source).
        if text.startswith("/") and " " not in text:
            prefix = text[1:]
            if picker_source is not None:
                matches = picker_source.rank(prefix)
                state["matches"] = [m.item for m in matches]
            else:
                # Legacy path — registry only, startswith filter.
                state["matches"] = [
                    c for c in SLASH_REGISTRY if c.name.startswith(prefix.lower())
                ][:20]
            state["selected_idx"] = 0
            state["mode"] = "slash"
            state["at_token_range"] = None
            return

        # @filepath mode — detect ``@<query>`` at cursor position.
        from opencomputer.cli_ui.file_completer import (
            extract_at_token,
            find_project_files,
            top_matches,
        )

        cursor = input_buffer.cursor_position
        token = extract_at_token(text, cursor)
        if token is not None:
            query, start, end = token
            # Empty query → show recent files. Non-empty → fuzzy match.
            files = find_project_files(Path.cwd())
            matches = top_matches(query, files, n=10)
            state["matches"] = matches
            state["selected_idx"] = 0
            state["mode"] = "file"
            state["at_token_range"] = (start, end)
            return

        state["matches"] = []
        state["selected_idx"] = 0
        state["mode"] = ""
        state["at_token_range"] = None

    def _on_text_changed(_buf):  # noqa: ANN001 — pt fires (sender,)
        _refilter(input_buffer.text)

    input_buffer.on_text_changed += _on_text_changed

    def _has_dropdown() -> bool:
        return bool(state["matches"])

    def _dropdown_text():
        return _render_dropdown_for_state(state)

    def _dropdown_height():
        # ``Dimension.exact(N)`` is the classmethod that builds a fixed-N
        # dimension. Earlier code used ``Dimension(exact=N)`` which is
        # invalid — the constructor only accepts ``min/max/weight/preferred``.
        # Calling it crashed prompt_toolkit's renderer the moment the user
        # typed ``/`` (PR #210 follow-up).
        # Cap raised from 10 → 20 in Task 8 to match the picker source's
        # default top_n. Skills + commands are mixed so 10 was too tight.
        return Dimension.exact(min(len(state["matches"]), 20))

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

    def _apply_selection() -> None:
        """Replace the active token (slash prefix or @<query>) with the
        selected dropdown row's expansion. Updates buffer + cursor.

        Slash mode handles both CommandDef (uses .name) and SkillEntry
        (uses .id) — the user's slash text is the same in both cases.
        """
        from .slash import CommandDef, SkillEntry

        if not state["matches"] or not (
            0 <= state["selected_idx"] < len(state["matches"])
        ):
            return
        sel = state["matches"][state["selected_idx"]]
        if state["mode"] == "file":
            start, end = state["at_token_range"]
            full = input_buffer.text
            insertion = f"@{sel}"
            new_text = full[:start] + insertion + full[end:]
            input_buffer.text = new_text
            input_buffer.cursor_position = start + len(insertion)
            # Re-filter so the dropdown updates after the insert (likely
            # closes since the cursor lands at end-of-token).
            _refilter(new_text)
        else:
            if isinstance(sel, CommandDef):
                slash_text = sel.name
            elif isinstance(sel, SkillEntry):
                slash_text = sel.id
            else:
                return
            input_buffer.text = f"/{slash_text}"
            input_buffer.cursor_position = len(input_buffer.text)

    @kb.add(Keys.ControlI, filter=Condition(_has_dropdown))  # Tab
    def _tab(event):  # noqa: ANN001
        _apply_selection()

    @kb.add(Keys.Enter)
    def _enter(event):  # noqa: ANN001
        # If dropdown is open and a row is selected, expand to that
        # command/path before submitting (so the row visibly chosen wins).
        if state["matches"] and 0 <= state["selected_idx"] < len(state["matches"]):
            if state["mode"] == "file":
                _apply_selection()
                # File completion expands inline; do NOT submit on the
                # same Enter — let the user keep editing or press Enter
                # again to send.
                return
            from .slash import CommandDef, SkillEntry

            sel = state["matches"][state["selected_idx"]]
            if isinstance(sel, CommandDef):
                slash_text = sel.name
            elif isinstance(sel, SkillEntry):
                slash_text = sel.id
            else:
                event.app.exit(result=input_buffer.text)
                return
            input_buffer.text = f"/{slash_text}"
            # Record the pick to MRU so it floats next session.
            try:
                mru_store.record(slash_text)
            except Exception:  # noqa: BLE001 — never break submit
                pass
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
        # Empty paste = clipboard-image attempt (existing flow)
        if not data.strip() and _try_attach_clipboard_image_into_buffer(
            event, profile_home=profile_home
        ):
            return

        # Paste-fold: long pastes get replaced with [Pasted text #N +M lines]
        # in the buffer; full content stored for submit-time expansion. If
        # the same content is pasted twice, expand the placeholder in the
        # buffer instead of inserting a second placeholder ("paste again
        # to expand").
        if paste_folder is not None and data:
            if paste_folder.is_same_as_last(data):
                ph = paste_folder.placeholder_for_last()
                buf_text = event.current_buffer.text
                if ph and ph in buf_text:
                    new_text = buf_text.replace(ph, data, 1)
                    event.current_buffer.text = new_text
                    event.current_buffer.cursor_position = len(new_text)
                    return
            folded, _bid = paste_folder.fold(data)
            event.current_buffer.insert_text(folded)
            return

        event.current_buffer.insert_text(data)

    # fzf-inspired aesthetic: bright cyan title for the highlighted row,
    # yellow ❯ cursor, no heavy bg blocks. Title indicator (right-aligned)
    # uses a dim cyan box mirroring Claude Code's session-name corner tag.
    style = Style.from_dict(
        {
            "prompt": "ansigreen bold",
            "dd.cursor": "bold #ffaf00",
            "dd.cursor.dim": "#3a3a3a",
            "dd.title": "#a8a8a8",
            "dd.title.selected": "bold #61afef",
            "dd.cat": "#5f87af",
            "dd.cat.selected": "bold #61afef",
            "dd.tag.command": "#5fafd7",  # cyan — built-in commands
            "dd.tag.skill": "#5faf5f",  # green — installed skills
            "dd.desc": "#6c6c6c",
            "dd.desc.selected": "#bcbcbc",
            "dd.divider": "#3a3a3a",
            "title.box": "#5fafd7",
            "title.text": "bold #5fafd7",
            "hint.dim": "italic #6c6c6c",
        }
    )

    prompt_window = Window(
        content=FormattedTextControl([("class:prompt", "you › ")]),
        height=1,
        dont_extend_width=True,
    )
    # ``wrap_lines=True`` makes long typed input wrap to a new visible
    # line at the right edge instead of horizontal-scrolling off-screen.
    # Combined with a flex height (``Dimension(min=1, max=10)``), the input
    # area grows downward as wrapping requires more lines, capping at 10
    # so it never eats the entire screen. The prompt_window stays
    # ``height=1`` and renders only on the first row — wrapped continuation
    # lines have no prefix, matching zsh/fish wrap UX.
    input_window = Window(
        content=BufferControl(buffer=input_buffer),
        height=Dimension(min=1, max=10),
        wrap_lines=True,
    )

    # Right-aligned session-title indicator above the input — mirrors the
    # cyan corner tag in Claude Code (e.g. "UI-changes"). Hidden when the
    # session has no manual title set.
    from prompt_toolkit.layout import WindowAlign

    def _title_text():
        if not session_title:
            return []
        return [
            ("class:title.box", "┤ "),
            ("class:title.text", session_title),
            ("class:title.box", " ├"),
        ]

    # Show the corner indicator only for sane-length titles (≤50 chars).
    # Existing sessions may have a runaway auto-generated title (the now-
    # disabled cheap-LLM titler sometimes returned the AI's greeting as
    # a "title" — see Image #12). Filter those out at the UI layer so
    # historical bad data doesn't surface.
    def _title_is_displayable() -> bool:
        return bool(session_title) and 1 <= len(session_title) <= 50

    title_window = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(_title_text),
            height=1,
            align=WindowAlign.RIGHT,
            dont_extend_height=True,
        ),
        filter=Condition(_title_is_displayable),
    )

    dropdown_window = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(_dropdown_text),
            height=_dropdown_height,
            wrap_lines=False,
        ),
        filter=Condition(_has_dropdown),
    )
    dropdown_divider = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(
                lambda: [("class:dd.divider", "─" * 80)]
            ),
            height=1,
        ),
        filter=Condition(_has_dropdown),
    )

    # CRITICAL: input row goes LAST in the HSplit. prompt_toolkit's
    # renderer positions the cursor at the focused control after drawing
    # the layout — if input is the last-drawn element, the cursor lands
    # there NATURALLY without needing a relative-up cursor move (\x1b[NA).
    # That relative move depends on Cursor-Position-Report which fails in
    # editor terminals (VS Code, JetBrains), so dropdown-below-input was
    # silently being overwritten by the misplaced cursor on every render.
    # Putting dropdown ABOVE input removes the dependency entirely.
    # Filler window at the TOP of the HSplit takes all remaining vertical
    # space, pushing the dropdown + divider + title + input row to the
    # bottom of the terminal. This makes the dropdown HUG the input
    # instead of floating disconnected at the top of the screen — the
    # conventional shell-completion UX (zsh autosuggest, fish, etc.)
    # without the CPR dependency that dropdown-below-input would need.
    filler = Window()

    # Paste-fold hint: dim "paste again to expand" line below the input,
    # only visible when the buffer contains a folded placeholder we know
    # about. Mirrors Claude Code's UX.
    def _paste_hint_text():
        if paste_folder is None or not paste_folder.has_active_fold(input_buffer.text):
            return []
        return [("class:hint.dim", "paste again to expand")]

    def _has_paste_hint() -> bool:
        return paste_folder is not None and paste_folder.has_active_fold(input_buffer.text)

    paste_hint_window = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(_paste_hint_text),
            height=1,
        ),
        filter=Condition(_has_paste_hint),
    )

    layout = Layout(
        HSplit(
            [
                filler,
                dropdown_window,
                dropdown_divider,
                title_window,
                VSplit([prompt_window, input_window]),
                paste_hint_window,
            ]
        ),
        focused_element=input_window,
    )

    # Construct the Output explicitly with enable_cpr=False so the
    # renderer never sends `\x1b[6n` and never trusts CPR responses.
    # Why: VS Code (and some JetBrains) terminals respond to CPR
    # *partially or with a delay*, which tricks prompt_toolkit's
    # renderer into using the CPR-dependent code path even when the
    # response is unreliable. Forcing enable_cpr=False makes the
    # renderer commit to the no-CPR fallback unconditionally — the
    # path that actually works in those terminals.
    import sys as _sys

    from prompt_toolkit.output.defaults import create_output as _create_output

    try:
        _output = _create_output(stdout=_sys.stdout)
        # The Vt100_Output instance from create_output has enable_cpr=True
        # baked in by default; we forcibly disable it post-construction by
        # patching the property's underlying flag. This is more robust
        # than constructing a fresh Vt100_Output ourselves because
        # create_output detects the right Output class for the current
        # platform (Windows uses a different class entirely).
        if hasattr(_output, "enable_cpr"):
            _output.enable_cpr = False  # type: ignore[attr-defined]
    except Exception:
        _output = None  # let Application pick the default

    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
        output=_output,
    )

    text = await app.run_async()
    return _strip_trailing_whitespace(text or "")
