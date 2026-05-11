"""Phase F — auto-name session from plan content on plan-mode accept.

Coverage:

    1. ``_session_name_from_plan_content`` strips markdown headers,
       bullets, numbered lists, trailing punctuation, and collapses
       whitespace.
    2. Returns ``""`` on empty / whitespace-only / chrome-only input.
    3. Caps long lines with ``…``.
    4. ``AgentLoop._maybe_auto_name_from_plan`` writes the title only
       when the current title is empty.
    5. ``AgentLoop._maybe_auto_name_from_plan`` survives DB errors
       without raising — chat must never crash on auto-name.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from opencomputer.agent.loop import _session_name_from_plan_content
from opencomputer.agent.state import SessionDB

# ─── _session_name_from_plan_content ─────────────────────────────────


def test_returns_empty_for_empty_input() -> None:
    assert _session_name_from_plan_content("") == ""


def test_returns_empty_for_whitespace_only() -> None:
    assert _session_name_from_plan_content("   \n  \t \n ") == ""


def test_returns_first_meaningful_line() -> None:
    plan = "\n\n  \nRefactor the auth module\nstep 2\nstep 3"
    assert _session_name_from_plan_content(plan) == "Refactor the auth module"


def test_strips_h1_markdown_header() -> None:
    plan = "# Migration plan\n\nDetails follow"
    assert _session_name_from_plan_content(plan) == "Migration plan"


def test_strips_multi_hash_header() -> None:
    plan = "### Sub-section title\nbody"
    assert _session_name_from_plan_content(plan) == "Sub-section title"


def test_strips_dash_bullet() -> None:
    plan = "- Step 1: refactor cache\n- Step 2: …"
    assert _session_name_from_plan_content(plan) == "Step 1: refactor cache"


def test_strips_asterisk_bullet() -> None:
    plan = "* First task\n* Second task"
    assert _session_name_from_plan_content(plan) == "First task"


def test_strips_plus_bullet() -> None:
    plan = "+ alpha\n+ beta"
    assert _session_name_from_plan_content(plan) == "alpha"


def test_strips_numbered_dot_list() -> None:
    plan = "1. Plan the migration\n2. Execute"
    assert _session_name_from_plan_content(plan) == "Plan the migration"


def test_strips_numbered_paren_list() -> None:
    plan = "1) Discovery\n2) Implementation"
    assert _session_name_from_plan_content(plan) == "Discovery"


def test_strips_double_digit_numbered_list() -> None:
    plan = "12. Last item\n13. Final"
    assert _session_name_from_plan_content(plan) == "Last item"


def test_strips_trailing_colon() -> None:
    plan = "Refactor the auth module:\nbody"
    assert _session_name_from_plan_content(plan) == "Refactor the auth module"


def test_strips_trailing_ellipsis() -> None:
    plan = "Investigating the cache bug…\nstep 2"
    assert _session_name_from_plan_content(plan) == "Investigating the cache bug"


def test_strips_trailing_three_dots() -> None:
    plan = "Investigating the cache bug...\nstep 2"
    assert _session_name_from_plan_content(plan) == "Investigating the cache bug"


def test_collapses_internal_whitespace() -> None:
    plan = "Step    1:\tdo  the  thing"
    # Trailing ":" is stripped → "Step 1: do the thing" - 1 → "Step 1: do the thing"?
    # Wait: ":" at end of FIRST WORD. Let me check the algorithm —
    # it strips trailing decoration of the WHOLE LINE, not per-token.
    # So we land at the post-whitespace-collapse string: "Step 1: do the thing"
    assert _session_name_from_plan_content(plan) == "Step 1: do the thing"


def test_caps_long_line_with_ellipsis() -> None:
    plan = "A" * 100
    result = _session_name_from_plan_content(plan)
    assert len(result) == 60
    assert result.endswith("…")


def test_respects_custom_max_len() -> None:
    plan = "A" * 100
    result = _session_name_from_plan_content(plan, max_len=10)
    assert len(result) == 10
    assert result.endswith("…")


def test_complex_real_world_plan() -> None:
    """A plan as Claude might emit it in plan mode."""
    plan = """# Migration Plan: Authentication Refactor

## Steps

1. Survey the existing auth middleware
2. Identify session-token storage paths
3. Implement new JWT issuer
"""
    assert (
        _session_name_from_plan_content(plan)
        == "Migration Plan: Authentication Refactor"
    )


def test_chrome_only_plan_returns_empty() -> None:
    """A plan that's ONLY markdown chrome with no real text → ``""``."""
    plan = "# \n## \n- \n"
    assert _session_name_from_plan_content(plan) == ""


# ─── AgentLoop._maybe_auto_name_from_plan (write side) ────────────────


class _FakeLoop:
    """Minimal stand-in for AgentLoop that exposes the auto-name path.

    We don't construct a real AgentLoop because it needs a provider,
    config, RuntimeContext, and a working tool registry — too much
    setup for a unit test of one small write helper. Instead we
    duplicate the relevant attrs and import the bound method via
    ``AgentLoop._maybe_auto_name_from_plan.__get__(self, AgentLoop)``.
    """

    def __init__(self, db: SessionDB, session_id: str) -> None:
        self.db = db
        self._current_session_id = session_id


def _run_auto_name(loop: _FakeLoop, plan_text: str) -> None:
    """Invoke the actual AgentLoop bound method against our fake."""
    from opencomputer.agent.loop import AgentLoop

    AgentLoop._maybe_auto_name_from_plan(loop, plan_text)  # type: ignore[arg-type]


def test_writes_title_when_session_is_untitled(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid)  # no title

    loop = _FakeLoop(db, sid)
    _run_auto_name(loop, "# Refactor auth module\nstep 2")

    assert db.get_session_title(sid) == "Refactor auth module"


def test_does_not_overwrite_existing_title(tmp_path: Path) -> None:
    """User set a name via /rename or oc -n — auto-name must NOT clobber it."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.set_session_title(sid, "my-explicit-name")
    db.ensure_session(sid)

    loop = _FakeLoop(db, sid)
    _run_auto_name(loop, "# Some other plan title")

    assert db.get_session_title(sid) == "my-explicit-name"


def test_skips_write_when_plan_yields_no_name(tmp_path: Path) -> None:
    """If the plan content sanitises to ``""``, no write happens."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid)

    loop = _FakeLoop(db, sid)
    _run_auto_name(loop, "")  # empty input

    title = db.get_session_title(sid)
    assert not title  # NULL or ""


def test_survives_db_error_during_get(tmp_path: Path) -> None:
    """get_session_title raising must NOT propagate (chat keeps running)."""

    class _BrokenDB:
        def get_session_title(self, _sid: str) -> str:
            raise RuntimeError("disk full")

        def set_session_title(self, *_a, **_kw) -> None:
            raise AssertionError("must not be called when get failed")

    loop = _FakeLoop(_BrokenDB(), uuid.uuid4().hex)  # type: ignore[arg-type]
    # Must NOT raise.
    _run_auto_name(loop, "# Some plan")


def test_survives_db_error_during_set(tmp_path: Path) -> None:
    """set_session_title raising must NOT propagate either."""

    class _BrokenDB:
        def get_session_title(self, _sid: str) -> str:
            return ""

        def set_session_title(self, *_a, **_kw) -> None:
            raise RuntimeError("disk full")

    loop = _FakeLoop(_BrokenDB(), uuid.uuid4().hex)  # type: ignore[arg-type]
    _run_auto_name(loop, "# Some plan")


def test_skips_when_session_id_empty() -> None:
    """No active session_id → no-op (defensive)."""

    class _UncalledDB:
        def get_session_title(self, *_a, **_kw) -> str:
            raise AssertionError("must not query the DB when sid is empty")

        def set_session_title(self, *_a, **_kw) -> None:
            raise AssertionError("must not write to the DB when sid is empty")

    loop = _FakeLoop(_UncalledDB(), session_id="")  # type: ignore[arg-type]
    _run_auto_name(loop, "# Some plan")
