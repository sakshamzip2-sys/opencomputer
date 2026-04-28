"""End-to-end tests for the resume picker — drives the actual full-screen
:class:`Application` through a piped input and verifies the returned
session id matches what the user "selected".

These tests are the answer to "do I really get the chat I picked?". They
simulate keypresses (Enter / Up / Down / Esc / typed search text) against
the live picker and assert the exact session-id that would flow into
``_run_chat_session``.

prompt_toolkit pattern reference: pass a :func:`create_pipe_input` and
:class:`DummyOutput` via :func:`create_app_session` so the picker runs in
a fully programmatic terminal — no real keyboard, no real screen.
"""
from __future__ import annotations

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from opencomputer.cli_ui.resume_picker import SessionRow, run_resume_picker


def _rows() -> list[SessionRow]:
    return [
        SessionRow(id="alpha-1234", title="Alpha", started_at=10.0, message_count=3),
        SessionRow(id="bravo-5678", title="Bravo", started_at=20.0, message_count=1),
        SessionRow(id="charlie-9", title="Charlie", started_at=30.0, message_count=7),
    ]


def _run_with_keys(rows: list[SessionRow], keys: bytes) -> str | None:
    """Run the picker with a scripted keypress sequence and return the
    selected session id (or None on cancel).
    """
    with create_pipe_input() as inp:
        inp.send_bytes(keys)
        with create_app_session(input=inp, output=DummyOutput()):
            return run_resume_picker(rows)


# ---- happy path: arrows + enter ---------------------------------------------


def test_enter_immediately_returns_first_row_id():
    """The first row is highlighted on open. Pressing Enter must
    return THAT row's id, not anything else."""
    assert _run_with_keys(_rows(), b"\r") == "alpha-1234"


def test_one_arrow_down_then_enter_returns_second_row_id():
    """↓ moves cursor to row index 1. Enter must return that row's id."""
    # \x1b[B is the ANSI sequence for Down arrow.
    assert _run_with_keys(_rows(), b"\x1b[B\r") == "bravo-5678"


def test_two_arrow_downs_then_enter_returns_third_row_id():
    assert _run_with_keys(_rows(), b"\x1b[B\x1b[B\r") == "charlie-9"


def test_down_past_end_clamps_to_last_row():
    """5 Down presses on a 3-row list must clamp at row 2 (Charlie)."""
    assert _run_with_keys(_rows(), b"\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B\r") == "charlie-9"


def test_down_then_up_returns_to_first_row():
    """↓ then ↑ must return Alpha (back at index 0)."""
    assert _run_with_keys(_rows(), b"\x1b[B\x1b[A\r") == "alpha-1234"


def test_up_at_top_clamps_to_first_row():
    """↑ at the first row must NOT wrap to last; stays at first."""
    assert _run_with_keys(_rows(), b"\x1b[A\r") == "alpha-1234"


# ---- cancel paths ------------------------------------------------------------


def test_escape_returns_none():
    """Esc must cancel and return None — caller falls back to fresh session."""
    assert _run_with_keys(_rows(), b"\x1b") is None


def test_ctrl_c_returns_none():
    """Ctrl+C must cancel and return None."""
    # \x03 is Ctrl+C (ETX).
    assert _run_with_keys(_rows(), b"\x03") is None


def test_empty_rows_returns_none_immediately():
    """No sessions in the DB → picker bails out without showing UI."""
    # Note: empty rows path doesn't even start the Application, so no
    # need to feed input.
    assert run_resume_picker([]) is None


# ---- search / filter --------------------------------------------------------


def test_search_filters_to_matching_row_then_enter_returns_its_id():
    """Type "brav" then Enter — only Bravo matches → Bravo's id returned.
    This is the case where the user knows the title and types a substring."""
    assert _run_with_keys(_rows(), b"brav\r") == "bravo-5678"


def test_search_id_prefix_returns_that_session_id():
    """Type the first chars of a session id — picker matches and returns it."""
    assert _run_with_keys(_rows(), b"alpha-12\r") == "alpha-1234"


def test_search_case_insensitive():
    """Title match must be case-insensitive (Alpha vs ALPHA vs alpha)."""
    assert _run_with_keys(_rows(), b"ALPHA\r") == "alpha-1234"


def test_search_no_match_then_enter_returns_none():
    """Type a query that matches nothing → Enter on empty filter → None
    (per the picker's _enter handler)."""
    assert _run_with_keys(_rows(), b"zzzz\r") is None


def test_search_then_arrow_picks_within_filtered_subset():
    """Type "ha" → matches Alpha + Charlie (both contain "ha" substring)
    but NOT Bravo. Down arrow then Enter must return Charlie's id (the
    second row of the FILTERED list, not the original list)."""
    assert _run_with_keys(_rows(), b"ha\x1b[B\r") == "charlie-9"


def test_search_clearing_with_backspace_restores_full_list():
    """Type "alpha" then backspace 5 times → empty query → all rows visible.
    Then Down arrow + Enter picks Bravo (row 1 of the full list)."""
    # \x7f is DEL/Backspace on most terminals; prompt_toolkit handles \x08 too.
    assert _run_with_keys(_rows(), b"alpha\x7f\x7f\x7f\x7f\x7f\x1b[B\r") == "bravo-5678"


# ---- single-row list edge cases ---------------------------------------------


def test_single_row_enter_returns_only_id():
    rows = [SessionRow(id="solo", title="Solo", started_at=0.0, message_count=1)]
    assert _run_with_keys(rows, b"\r") == "solo"


def test_single_row_down_then_enter_still_returns_only_id():
    """Down on a 1-row list clamps to row 0 (no movement)."""
    rows = [SessionRow(id="solo", title="Solo", started_at=0.0, message_count=1)]
    assert _run_with_keys(rows, b"\x1b[B\r") == "solo"


# ---- weird-but-real metadata edge cases -------------------------------------


def test_row_with_empty_title_still_resumes_correctly():
    """An untitled session has title=='' — picker shows fallback "(untitled · <id>)"
    but Enter still returns the right id."""
    rows = [
        SessionRow(id="untitled-1", title="", started_at=0.0, message_count=0),
        SessionRow(id="titled-2", title="Has a Title", started_at=1.0, message_count=2),
    ]
    assert _run_with_keys(rows, b"\r") == "untitled-1"


def test_long_titles_dont_break_selection():
    """A 200-char title must still be selectable by Enter."""
    rows = [
        SessionRow(
            id="lng",
            title="x" * 200,
            started_at=0.0,
            message_count=1,
        ),
    ]
    assert _run_with_keys(rows, b"\r") == "lng"


def test_unicode_title_searchable():
    """Title with non-ASCII chars must still be searchable + selectable."""
    rows = [
        SessionRow(id="emoji-1", title="🚀 launch session", started_at=0.0, message_count=1),
        SessionRow(id="ascii-2", title="boring", started_at=0.0, message_count=1),
    ]
    assert _run_with_keys(rows, b"launch\r") == "emoji-1"


# ---- integration: resolve target → picker → id propagation -------------------


def test_resolve_resume_target_pick_returns_picker_result(monkeypatch, tmp_path):
    """End-to-end: ``_resolve_resume_target("pick")`` must return EXACTLY
    the session id the picker resolved to. Pin this so a future refactor
    can't accidentally swap rows or mangle the id on the way out."""
    import time

    from opencomputer import cli
    from opencomputer.agent.config import default_config as real_default_config
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    now = time.time()
    for i, sid in enumerate(["aaa-1", "bbb-2", "ccc-3"]):
        db.create_session(
            session_id=sid, platform="cli", model="m", title=f"sess {i}"
        )
        with db._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (now - i, sid),
            )

    def fake_default_config():
        from dataclasses import replace

        cfg = real_default_config()
        return replace(cfg, session=replace(cfg.session, db_path=db_path))

    monkeypatch.setattr(
        "opencomputer.agent.config.default_config", fake_default_config
    )

    # Drive the picker with: Down, Enter → user picks row index 1 = bbb-2.
    with create_pipe_input() as inp:
        inp.send_bytes(b"\x1b[B\r")
        with create_app_session(input=inp, output=DummyOutput()):
            resolved = cli._resolve_resume_target("pick")

    assert resolved == "bbb-2", (
        f"resolver must propagate the picker's selection unchanged; got {resolved!r}"
    )
