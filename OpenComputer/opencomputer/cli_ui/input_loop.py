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
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from opencomputer.cli_ui.clipboard import has_clipboard_image, save_clipboard_image
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
        # ESC during *idle* prompt: clear the buffer (matches Claude Code).
        # ESC during *streaming*: handled by KeyboardListener thread; the
        # prompt isn't the active app at that point.
        event.current_buffer.text = ""

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

    return PromptSession(
        message=HTML("<ansigreen><b>you ›</b></ansigreen> "),
        history=FileHistory(str(history_path)),
        key_bindings=kb,
        multiline=False,
        mouse_support=False,
        enable_history_search=True,
        complete_while_typing=False,
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
    """Read one line of user input via the prompt session.

    Returns the trimmed string. Caller handles ``EOFError`` (Ctrl+D)
    and ``KeyboardInterrupt`` (Ctrl+C with empty buffer).
    """
    session = build_prompt_session(profile_home=profile_home, scope=scope)
    text = await session.prompt_async()
    return _strip_trailing_whitespace(text or "")
