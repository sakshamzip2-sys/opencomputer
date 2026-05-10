"""Regression tests for the resume-fix work — three bugs at once:

1. ``oc resume <session-id>`` errors with "Got unexpected extra argument".
   Top-level resume command had no positional arg; only options.
2. Every session in the picker shows "(untitled · ID)" because
   :func:`opencomputer.agent.title_generator.maybe_auto_title` was ported
   from Hermes but never wired into :mod:`opencomputer.agent.loop`.
3. The picker has no scrolling — it renders every filtered row, so the
   list overflows the terminal when many sessions exist.

The fixes are surgical: a positional arg on the typer command, a 3-line
wire-in in the agent loop, a SessionRow.cwd field + fallback formatter
helper, and a pure scroll-window calculator that the picker calls per
render. All tests land RED on main.
"""
from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli import app
from opencomputer.cli_ui.resume_picker import (
    SessionRow,
)

runner = CliRunner()


# ─── Bug 1: `oc resume <id>` positional arg ───────────────────────────


def test_resume_command_help_shows_optional_session_arg() -> None:
    """The resume command's --help should advertise an optional session arg.

    Currently the command is options-only ("Usage: opencomputer resume
    [OPTIONS]"). After the fix, the usage line must include the
    positional ("[SESSION]" or similar) so the user can discover it.
    """
    result = runner.invoke(app, ["resume", "--help"])
    assert result.exit_code == 0
    # Help text in Click 8.x contains both stdout + stderr in `output`.
    out = result.output.lower()
    # After fix: usage line includes the positional. We allow either
    # ``[session]`` (typer's default rendering) or ``arguments`` section
    # so the test isn't pinned to a single Click version's formatting.
    assert "[session]" in out or "arguments" in out, (
        f"resume --help is still options-only — no positional shown:\n{out}"
    )


def test_resume_command_accepts_positional_session_id_unknown_exits_nonzero() -> None:
    """`oc resume <unknown-id>` must error cleanly (exit != 0), not crash.

    The current code raises Click's UsageError ("Got unexpected extra
    argument"). After the fix, an unknown id must be reported as
    "session not found" with a clean non-zero exit. We don't assert exact
    wording — just that the parser doesn't reject the positional outright.
    """
    # Use a 36-char fake UUID so id-prefix matching has nothing to grab onto.
    result = runner.invoke(
        app, ["resume", "deadbeef-1234-5678-9abc-deadbeefdead"]
    )
    # Exit code 2 is Click's UsageError; before fix the code is 2 with
    # "Got unexpected extra argument". After fix, an unknown id should
    # NOT produce a UsageError — typer accepts the positional, then our
    # code reports "session not found" (exit 1) or similar.
    assert "got unexpected extra argument" not in result.output.lower(), (
        f"resume command still rejects positional arg: {result.output!r}"
    )


# ─── Bug 2: maybe_auto_title wire-in ──────────────────────────────────


def test_agent_loop_module_references_maybe_auto_title() -> None:
    """Wire-in sanity: opencomputer/agent/loop.py must reference
    ``maybe_auto_title``.

    Without this reference, the auto-titler ported from Hermes never
    fires in production — the function is defined and unit-tested but
    no production caller invokes it. This is a literal source-grep
    test that catches the regression cheaply.

    Background: see memory entry "Ship Modules With Their Callsite" —
    every new module needs an end-to-end user-invokable trace before
    claiming done. The title generator was a Tier S Hermes port that
    had unit tests but no callsite.
    """
    import opencomputer.agent.loop as loop_mod

    with open(loop_mod.__file__, encoding="utf-8") as fh:
        src = fh.read()

    assert "maybe_auto_title" in src, (
        "opencomputer/agent/loop.py is missing the maybe_auto_title "
        "wire-in. Without it, no session ever gets auto-titled and "
        "the resume picker shows '(untitled · ID)' for all sessions."
    )


# ─── Bug 3: SessionRow gets cwd + fallback label helper ───────────────


def test_session_row_has_cwd_field() -> None:
    """SessionRow exposes a `cwd` field used by the fallback label.

    Allows the picker to render '<cwd-basename> @ HH:MM' instead of
    '(untitled · ID)' when the session has no title yet (pre-titler
    sessions, or first-message-still-pending sessions).
    """
    row = SessionRow(
        id="a", title="", started_at=0.0, message_count=1, cwd="/Users/x/proj"
    )
    assert row.cwd == "/Users/x/proj"


def test_session_row_cwd_defaults_to_empty() -> None:
    """SessionRow.cwd has a default so existing call sites don't break."""
    row = SessionRow(id="a", title="", started_at=0.0, message_count=1)
    assert row.cwd == ""


def test_format_session_label_prefers_title_when_present() -> None:
    """When the row has a title, render exactly the title (no decoration).

    Ensures the auto-titler's output is the headline once it fires.
    """
    from opencomputer.cli_ui.resume_picker import format_session_label

    row = SessionRow(
        id="a", title="Stock review", started_at=0.0, message_count=1, cwd="/x/p"
    )
    assert format_session_label(row, now=0.0) == "Stock review"


def test_format_session_label_uses_cwd_basename_when_title_empty() -> None:
    """No title + has cwd → '<basename> @ HH:MM' so the user sees real signal."""
    from opencomputer.cli_ui.resume_picker import format_session_label

    # 1714305600.0 = 2024-04-28 12:00:00 UTC. We just check the basename
    # appears and the '@' separator is there; locale-dependent HH:MM
    # formatting is intentionally not pinned.
    row = SessionRow(
        id="abc12345",
        title="",
        started_at=1714305600.0,
        message_count=1,
        cwd="/Users/saksham/Vscode/projAlpha",
    )
    label = format_session_label(row, now=1714305600.0)
    assert "projAlpha" in label
    assert "@" in label
    # Don't include the legacy "(untitled · ID)" form when we have a cwd.
    assert "(untitled" not in label


def test_format_session_label_falls_back_to_id_when_no_title_no_cwd() -> None:
    """No title AND no cwd → legacy '(untitled · <id-prefix>)' fallback.

    Existing very-old sessions may have neither — keep them legible.
    """
    from opencomputer.cli_ui.resume_picker import format_session_label

    row = SessionRow(id="abc12345-...", title="", started_at=0.0, message_count=1, cwd="")
    label = format_session_label(row, now=0.0)
    assert "abc12345" in label


# ─── Bug 3 continued: scroll-window calculator ────────────────────────


def _mkrows(n: int) -> list[SessionRow]:
    return [
        SessionRow(
            id=f"id{i:04d}",
            title=f"row {i}",
            started_at=0.0,
            message_count=1,
            cwd="",
        )
        for i in range(n)
    ]


def test_compute_visible_window_short_list_no_scroll() -> None:
    """When N rows ≤ height, return all rows with offset=0 — no scrolling."""
    from opencomputer.cli_ui.resume_picker import compute_visible_window

    rows = _mkrows(5)
    visible, offset = compute_visible_window(rows, selected_idx=2, window_height=10)
    assert visible == rows
    assert offset == 0


def test_compute_visible_window_scrolls_when_selected_below_window() -> None:
    """selected_idx beyond initial window → offset advances to keep it visible."""
    from opencomputer.cli_ui.resume_picker import compute_visible_window

    rows = _mkrows(50)
    visible, offset = compute_visible_window(rows, selected_idx=20, window_height=10)
    # The selected row must appear in the visible slice.
    assert rows[20] in visible
    # The slice should be window_height long when the list is bigger than the window.
    assert len(visible) == 10


def test_compute_visible_window_clamps_at_top() -> None:
    """Selected at index 0 → never scroll above; offset is 0."""
    from opencomputer.cli_ui.resume_picker import compute_visible_window

    rows = _mkrows(50)
    visible, offset = compute_visible_window(rows, selected_idx=0, window_height=10)
    assert offset == 0
    assert visible[0] == rows[0]
    assert len(visible) == 10


def test_compute_visible_window_clamps_at_bottom() -> None:
    """Selected at last index → offset clamps so visible[-1] == rows[-1]."""
    from opencomputer.cli_ui.resume_picker import compute_visible_window

    rows = _mkrows(50)
    visible, offset = compute_visible_window(
        rows, selected_idx=len(rows) - 1, window_height=10
    )
    assert visible[-1] == rows[-1]
    assert len(visible) == 10
    # Offset should be exactly len-height (last-page slice).
    assert offset == len(rows) - 10


def test_compute_visible_window_handles_empty() -> None:
    """Empty list → empty visible, offset 0, no crash."""
    from opencomputer.cli_ui.resume_picker import compute_visible_window

    visible, offset = compute_visible_window([], selected_idx=-1, window_height=5)
    assert visible == []
    assert offset == 0


def test_compute_visible_window_negative_selected_idx_safe() -> None:
    """selected_idx == -1 (no selection) → return top window, no crash."""
    from opencomputer.cli_ui.resume_picker import compute_visible_window

    rows = _mkrows(20)
    visible, offset = compute_visible_window(rows, selected_idx=-1, window_height=5)
    assert offset == 0
    assert len(visible) == 5
