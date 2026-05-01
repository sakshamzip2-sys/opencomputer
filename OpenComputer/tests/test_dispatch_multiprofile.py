"""End-to-end Phase 3: BindingResolver routes events to the right profile,
two profiles can dispatch concurrently, ContextVar isolated per task,
long-running tool tasks carry their creation-time profile (Pass-2 F10).

Pass-1 G5 fix: this test uses asyncio.Event for explicit synchronization
instead of sleep timing — flake-proof on loaded CI.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
)
from opencomputer.gateway.agent_router import AgentRouter
from opencomputer.gateway.binding_resolver import BindingResolver
from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk.core import MessageEvent, Platform
from plugin_sdk.profile_context import current_profile_home, set_profile


def _ev(chat_id: str, **kwargs) -> MessageEvent:
    """MessageEvent factory that handles all required fields."""
    # Some test runs expect user_id and timestamp on MessageEvent — adjust
    # if your build's MessageEvent dataclass omits them.
    base = dict(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        text=kwargs.pop("text", "hi"),
        attachments=[],
        metadata=kwargs.pop("metadata", {}),
    )
    base.update(kwargs)
    try:
        return MessageEvent(**base)
    except TypeError:
        # Older builds may require user_id / timestamp
        base.setdefault("user_id", "u0")
        base.setdefault("timestamp", 0.0)
        return MessageEvent(**base)


@pytest.mark.asyncio
async def test_two_chats_two_profiles_run_in_parallel(tmp_path: Path) -> None:
    """Pass-1 G5 fix: use asyncio.Event for explicit sync, not sleep timing.

    If serial dispatch, profile_a's wait blocks profile_b from running →
    asyncio.wait_for raises TimeoutError → test fails loudly.
    """
    started: list[str] = []
    finished: list[str] = []
    b_in_flight = asyncio.Event()
    a_unblocked = asyncio.Event()

    def make_loop(pid: str, home: Path) -> MagicMock:
        m = MagicMock(name=f"loop-{pid}")

        async def run_a(user_message: str, session_id: str, **kw):
            started.append("a")
            # Block until profile 'b' is in-flight. If dispatch is
            # SERIAL, this never happens → asyncio.wait_for raises.
            await asyncio.wait_for(b_in_flight.wait(), timeout=2.0)
            a_unblocked.set()
            finished.append("a")
            return MagicMock(final_message=MagicMock(content="reply-a"))

        async def run_b(user_message: str, session_id: str, **kw):
            started.append("b")
            b_in_flight.set()
            # Wait for A to unblock so finished order is deterministic
            # AND A is provably not blocked on us.
            await asyncio.wait_for(a_unblocked.wait(), timeout=2.0)
            finished.append("b")
            return MagicMock(final_message=MagicMock(content="reply-b"))

        m.run_conversation = run_a if pid == "a" else run_b
        m._consent_gate = None
        return m

    router = AgentRouter(
        loop_factory=make_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    cfg = BindingsConfig(
        default_profile="default",
        bindings=(
            Binding(match=BindingMatch(chat_id="A"), profile="a"),
            Binding(match=BindingMatch(chat_id="B"), profile="b"),
        ),
    )
    resolver = BindingResolver(cfg)
    dispatch = Dispatch(router=router, resolver=resolver)

    ev_a = _ev(chat_id="A")
    ev_b = _ev(chat_id="B")

    await asyncio.gather(dispatch.handle_message(ev_a), dispatch.handle_message(ev_b))

    # If serial, A's wait would have timed out. Reaching this point at
    # all proves parallelism. Order is deterministic by the event chain.
    assert finished == ["a", "b"], (
        f"got {finished}; expected ['a', 'b'] under parallel dispatch"
    )


@pytest.mark.asyncio
async def test_contextvar_isolated_per_dispatch(tmp_path: Path) -> None:
    """Each per-profile dispatch sees its own current_profile_home."""
    seen_homes: dict[str, Path | None] = {}

    def make_loop(pid: str, home: Path) -> MagicMock:
        m = MagicMock()
        m._consent_gate = None

        async def run(user_message: str, session_id: str, **kw):
            seen_homes[pid] = current_profile_home.get()
            return MagicMock(final_message=MagicMock(content="ok"))

        m.run_conversation = run
        return m

    router = AgentRouter(
        loop_factory=make_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    cfg = BindingsConfig(
        default_profile="default",
        bindings=(
            Binding(match=BindingMatch(chat_id="A"), profile="a"),
            Binding(match=BindingMatch(chat_id="B"), profile="b"),
        ),
    )
    dispatch = Dispatch(router=router, resolver=BindingResolver(cfg))
    ev_a = _ev(chat_id="A")
    ev_b = _ev(chat_id="B")

    await asyncio.gather(dispatch.handle_message(ev_a), dispatch.handle_message(ev_b))
    assert seen_homes["a"] == tmp_path / "a"
    assert seen_homes["b"] == tmp_path / "b"


@pytest.mark.asyncio
async def test_long_running_tool_task_carries_creation_time_profile(tmp_path: Path) -> None:
    """Pass-2 F10 contract: a tool that creates an asyncio.Task during dispatch
    captures the ContextVar at task-creation time. The bg task continues to
    resolve to the original profile even after the dispatch ends and a
    different profile dispatches.

    Pinning this contract: future refactors that "optimize" by clearing
    ContextVar mid-flight would break it. Acceptable as-is for v1; the
    test makes the inheritance behavior explicit.
    """
    seen_in_bg: list[Path | None] = []
    bg_done = asyncio.Event()

    async def long_running_task() -> None:
        # Snapshot at task creation; runs after dispatch ends
        await asyncio.sleep(0.05)
        v = current_profile_home.get()
        seen_in_bg.append(v)
        bg_done.set()

    profile_a = tmp_path / "a"
    profile_a.mkdir()
    profile_b = tmp_path / "b"
    profile_b.mkdir()

    # Simulate a tool creating a bg task during dispatch under profile A.
    with set_profile(profile_a):
        task = asyncio.create_task(long_running_task())

    # Now do a dispatch under profile B (simulated by entering set_profile(b)).
    with set_profile(profile_b):
        await asyncio.sleep(0.001)

    # Wait for the bg task — it ran AFTER profile B set_profile.
    await asyncio.wait_for(bg_done.wait(), timeout=2.0)

    # The bg task captured profile_a at creation time. Per asyncio.Task
    # contextvars contract, it sees profile_a regardless of what set_profile
    # was active at the moment it ran.
    assert seen_in_bg == [profile_a], (
        f"expected bg task to keep profile_a contextvar at creation; got {seen_in_bg}"
    )
    # Awaiting the task to keep ruff happy and ensure clean shutdown.
    await task


@pytest.mark.asyncio
async def test_default_profile_when_no_match(tmp_path: Path) -> None:
    """An event that doesn't match any binding routes to default_profile."""
    routed: list[str] = []

    def make_loop(pid: str, home: Path) -> MagicMock:
        m = MagicMock()
        m._consent_gate = None
        async def run(user_message, session_id, **kw):
            routed.append(pid)
            return MagicMock(final_message=MagicMock(content="ok"))
        m.run_conversation = run
        return m

    router = AgentRouter(
        loop_factory=make_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    cfg = BindingsConfig(
        default_profile="myhome",
        bindings=(
            Binding(match=BindingMatch(chat_id="A"), profile="a"),
        ),
    )
    dispatch = Dispatch(router=router, resolver=BindingResolver(cfg))

    # Event with chat_id that doesn't match any binding
    await dispatch.handle_message(_ev(chat_id="UNMATCHED"))
    assert routed == ["myhome"]


@pytest.mark.asyncio
async def test_resolver_can_be_none_falls_back_to_default(tmp_path: Path) -> None:
    """If resolver=None (legacy path), profile_id defaults to 'default'."""
    routed: list[str] = []

    def make_loop(pid: str, home: Path) -> MagicMock:
        m = MagicMock()
        m._consent_gate = None
        async def run(user_message, session_id, **kw):
            routed.append(pid)
            return MagicMock(final_message=MagicMock(content="ok"))
        m.run_conversation = run
        return m

    router = AgentRouter(
        loop_factory=make_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    # Pre-seed default loop so the test doesn't actually need the factory
    fake = make_loop("default", tmp_path / "default")
    router._loops["default"] = fake

    # No resolver passed
    dispatch = Dispatch(router=router)  # resolver=None
    await dispatch.handle_message(_ev(chat_id="anything"))
    assert routed == ["default"]
