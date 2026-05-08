"""Gateway refuses /goal <text> when a run is active for the session.

Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §3 Gap D.

The check lives on Dispatch._goal_midrun_check + is invoked from
_maybe_bypass_running_guard before the bypass_running_guard gate. We
test the helper in isolation here; integration through
_maybe_bypass_running_guard is exercised through downstream gateway
test suites which already build full Dispatch instances.
"""
from __future__ import annotations

import pytest

from opencomputer.gateway import dispatch as disp


def _bare_dispatch() -> disp.Dispatch:
    """Build a Dispatch shell without booting plugins / event loop."""
    d = disp.Dispatch.__new__(disp.Dispatch)
    d._active_runs = set()
    return d


@pytest.mark.asyncio
async def test_refuses_set_form_when_run_active():
    d = _bare_dispatch()
    sid = "s_locked"
    d._active_runs.add(sid)

    refused = await d._goal_midrun_check(
        session_id=sid, args=["new", "goal"]
    )
    assert refused is not None
    assert "/stop" in refused


@pytest.mark.asyncio
async def test_allows_set_form_when_idle():
    d = _bare_dispatch()
    refused = await d._goal_midrun_check(
        session_id="s_idle", args=["new", "goal"]
    )
    assert refused is None


@pytest.mark.asyncio
async def test_allows_status_while_running():
    d = _bare_dispatch()
    sid = "s_locked"
    d._active_runs.add(sid)
    refused = await d._goal_midrun_check(session_id=sid, args=["status"])
    assert refused is None


@pytest.mark.asyncio
async def test_allows_pause_resume_clear_while_running():
    d = _bare_dispatch()
    sid = "s_locked"
    d._active_runs.add(sid)
    for sub in ("pause", "resume", "clear"):
        assert await d._goal_midrun_check(
            session_id=sid, args=[sub]
        ) is None


@pytest.mark.asyncio
async def test_allows_no_args_treated_as_status():
    d = _bare_dispatch()
    sid = "s_locked"
    d._active_runs.add(sid)
    assert await d._goal_midrun_check(session_id=sid, args=[]) is None


@pytest.mark.asyncio
async def test_session_isolation_one_running_doesnt_block_other():
    """Mid-run guard is per-session — set form on session B is fine."""
    d = _bare_dispatch()
    d._active_runs.add("s_a")
    refused = await d._goal_midrun_check(
        session_id="s_b", args=["new", "goal"]
    )
    assert refused is None
