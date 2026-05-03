"""Regression tests for the slash-picker blank-gap bug.

Bug: after the user selected a command via Tab, or dismissed the
dropdown via Escape, the dropdown's reserved screen rows stayed
on-screen as a blank gap until the next keystroke triggered a
re-render (via on_text_changed).

Root cause: the slash-mode branch of ``_apply_selection`` did NOT call
``_refilter`` after writing the buffer (the file-completion branch
did). And the Escape handler mutated state without calling
``event.app.invalidate()``, which the sibling Shift+Tab and Ctrl+P
handlers DO call after their state mutations.

These tests verify the fix at the source level — the Application layer
is hard to drive headlessly (see test_input_loop_skill_picker.py
docstring), but `inspect.getsource` is sufficient to lock in the
contract.
"""
from __future__ import annotations

import inspect
import re

from opencomputer.cli_ui import input_loop


def _read_user_input_source() -> str:
    return inspect.getsource(input_loop.read_user_input)


def test_apply_selection_slash_mode_calls_refilter_after_buffer_write():
    """The slash branch (else: in _apply_selection) MUST call
    _refilter(input_buffer.text) after writing the buffer so the
    dropdown collapses immediately. Without this, the dropdown's
    reserved rows stay on-screen as a blank gap."""
    src = _read_user_input_source()
    pattern = re.compile(
        r'input_buffer\.text\s*=\s*f"/\{slash_text\}".*?'
        r"_refilter\(",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "_apply_selection slash branch must call _refilter() after "
        "writing the buffer — otherwise the dropdown stays on-screen "
        "as a blank gap until the next keystroke"
    )


def test_tab_handler_calls_invalidate():
    """The Tab key handler MUST call event.app.invalidate() after
    _apply_selection() so the layout re-renders the now-empty
    dropdown ConditionalContainer immediately."""
    src = _read_user_input_source()
    # Look for def _tab(event): ... _apply_selection() ... invalidate() ...
    # before the next inner-function definition (def _shift_tab) so we
    # don't accidentally match across handlers.
    pattern = re.compile(
        r"def _tab\(event\):.*?"
        r"_apply_selection\(\).*?"
        r"event\.app\.invalidate\(\).*?"
        r"def _shift_tab",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "_tab handler must call event.app.invalidate() after "
        "_apply_selection() — otherwise the dropdown stays on-screen"
    )


def test_escape_handler_invalidates_after_clearing_matches():
    """The Escape key handler MUST call event.app.invalidate() after
    mutating state (clearing matches) so the layout re-renders the
    now-empty dropdown. Sibling handlers Shift+Tab and Ctrl+P already
    do this — Escape was the inconsistent one."""
    src = _read_user_input_source()
    pattern = re.compile(
        r'state\["matches"\]\s*=\s*\[\]\s*\n'
        r'\s*state\["selected_idx"\]\s*=\s*0\s*\n'
        r"\s*.*?event\.app\.invalidate\(\)",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "Escape handler must call event.app.invalidate() after "
        "clearing state['matches'] — otherwise the dropdown's reserved "
        "rows stay on-screen as a blank gap until the next keystroke"
    )


def test_shift_tab_still_invalidates():
    """Sanity check: the existing Shift+Tab handler still calls
    invalidate() — making sure the fix didn't break the pattern we
    were matching."""
    src = _read_user_input_source()
    pattern = re.compile(
        r"@kb\.add\(Keys\.BackTab\).*?def _shift_tab.*?event\.app\.invalidate\(\)",
        re.DOTALL,
    )
    assert pattern.search(src), "Shift+Tab handler must still invalidate"


def test_ctrl_p_still_invalidates():
    """Sanity check: the existing Ctrl+P handler still calls
    invalidate() — same reason."""
    src = _read_user_input_source()
    pattern = re.compile(
        r"@kb\.add\(Keys\.ControlP\).*?def _ctrl_p.*?event\.app\.invalidate\(\)",
        re.DOTALL,
    )
    assert pattern.search(src), "Ctrl+P handler must still invalidate"
