"""Gateway refuses /goal <text> when a run is active for the session.

Spec: docs/superpowers/specs/2026-05-08-kanban-goals-v2-design.md §3 Gap D.

The check lives on Dispatch._goal_midrun_check + is invoked from
_maybe_bypass_running_guard before the bypass_running_guard gate. We
test the helper in isolation here; integration through
_maybe_bypass_running_guard is exercised through downstream gateway
test suites which already build full Dispatch instances.
"""
from __future__ import annotations

import asyncio

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


# ─── Banner forwarding to chat (Task 14 follow-through) ─────────────────


@pytest.mark.asyncio
async def test_install_goal_banner_callback_routes_to_adapter():
    """The callback installed by _install_goal_banner_callback should
    schedule adapter.send when the loop fires a goal banner."""
    from opencomputer.agent.goal import GoalState, JudgeVerdict

    d = _bare_dispatch()

    sent: list[tuple[str, str]] = []

    class _StubAdapter:
        async def send(self, chat_id: str, text: str) -> None:
            sent.append((chat_id, text))

    class _StubLoop:
        def __init__(self) -> None:
            self._cbs: dict = {}

        def set_goal_banner_callback(self, sid, cb):
            self._cbs[sid] = cb

        def clear_goal_banner_callback(self, sid):
            self._cbs.pop(sid, None)

    loop = _StubLoop()
    adapter = _StubAdapter()
    sid = "s_route"

    d._install_goal_banner_callback(
        loop=loop, session_id=sid, adapter=adapter, chat_id="chat-1",
    )
    assert sid in loop._cbs

    # Fire a continue banner — the registered cb should schedule send.
    cb = loop._cbs[sid]
    cb(
        session_id=sid,
        kind="continue",
        verdict=JudgeVerdict(done=False, reason="2 of 4 done"),
        goal=GoalState(text="x", turns_used=1, budget=20),
    )
    # Async send is scheduled via run_coroutine_threadsafe; allow the
    # event loop one tick to drain it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(sent) == 1
    chat_id, text = sent[0]
    assert chat_id == "chat-1"
    assert "↻" in text and "1/20" in text and "2 of 4 done" in text


@pytest.mark.asyncio
async def test_install_goal_banner_callback_swallows_send_errors():
    """Banner callback must never wedge the agent loop."""
    from opencomputer.agent.goal import GoalState, JudgeVerdict

    d = _bare_dispatch()

    class _BoomAdapter:
        async def send(self, chat_id: str, text: str) -> None:
            raise RuntimeError("network down")

    class _StubLoop:
        def __init__(self) -> None:
            self._cbs: dict = {}

        def set_goal_banner_callback(self, sid, cb):
            self._cbs[sid] = cb

        def clear_goal_banner_callback(self, sid):
            self._cbs.pop(sid, None)

    loop = _StubLoop()
    sid = "s_boom"
    d._install_goal_banner_callback(
        loop=loop, session_id=sid, adapter=_BoomAdapter(), chat_id="c",
    )
    cb = loop._cbs[sid]
    # Should not raise.
    cb(
        session_id=sid,
        kind="achieved",
        verdict=JudgeVerdict(done=True, reason="ok"),
        goal=GoalState(text="x", turns_used=4, budget=20),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # No assertion needed beyond "didn't raise" — _safe_send_goal_banner
    # logs and swallows.
