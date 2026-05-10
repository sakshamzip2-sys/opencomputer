"""CC §2 — verify each new event has a real firing site.

Adding an enum without a producer is dishonest "support". These tests
register a handler against each new event and exercise the natural
firing path to assert the event actually fires.

Spec: docs/OC-FROM-CLAUDE-CODE.md §2.

Coverage:
  - POST_TOOL_BATCH       — AgentLoop after _dispatch_tool_calls
  - USER_PROMPT_EXPANSION — slash_dispatcher after a slash produces output
  - INSTRUCTIONS_LOADED   — AgentLoop on first run_conversation per sid
  - CWD_CHANGED           — AgentLoop on os.getcwd() drift between turns
  - FILE_CHANGED          — plugin-driven (no core emitter); contract
                            verified by enum + dataclass shape only
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.slash_dispatcher import dispatch
from opencomputer.hooks.engine import engine as hook_engine
from plugin_sdk.hooks import HookContext, HookEvent, HookSpec
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


@pytest.fixture
def captured_events(monkeypatch):
    """Subscribe to all CC §2 events on the live hook engine. Yields
    a list that receives one entry per fire; tests assert against
    its content. We deregister after the test so a re-run doesn't
    accumulate handlers."""
    captured: list[HookContext] = []

    async def _handler(ctx: HookContext) -> None:
        captured.append(ctx)

    cc2_events = (
        HookEvent.POST_TOOL_BATCH,
        HookEvent.USER_PROMPT_EXPANSION,
        HookEvent.INSTRUCTIONS_LOADED,
        HookEvent.CWD_CHANGED,
        HookEvent.FILE_CHANGED,
    )
    # Use private slot directly so we can clean up cleanly. Falls back
    # to public register/unregister if the engine grows them later.
    snapshots: list = []
    for ev in cc2_events:
        spec = HookSpec(event=ev, handler=_handler, fire_and_forget=True)
        hook_engine.register(spec)
        snapshots.append(spec)
    try:
        yield captured
    finally:
        # Best-effort unregister; if the engine drops support, the
        # next test fixture re-init clears state anyway.
        unregister = getattr(hook_engine, "unregister", None)
        if callable(unregister):
            for s in snapshots:
                try:
                    unregister(s)
                except Exception:  # noqa: BLE001 — cleanup is best-effort
                    pass


# ─── USER_PROMPT_EXPANSION ────────────────────────────────────────────────


class _ExpandingSlash(SlashCommand):
    name = "expand-me"
    description = "test slash that returns synthetic prompt text"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        return SlashCommandResult(
            output=f"expanded text for {args!r}", handled=True
        )


class _SilentSlash(SlashCommand):
    name = "silent"
    description = "test slash with empty output (side-effect only)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        return SlashCommandResult(output="", handled=True)


@pytest.mark.asyncio
async def test_user_prompt_expansion_fires_on_slash_with_output(captured_events):
    """A slash that returns non-empty output triggers UserPromptExpansion."""
    runtime = RuntimeContext(custom={"session_id": "sess-test"})
    commands = {"expand-me": _ExpandingSlash()}
    await dispatch("/expand-me hello world", commands, runtime)

    # Wait briefly for fire-and-forget tasks to drain.
    await asyncio.sleep(0.05)
    upe_events = [
        c for c in captured_events if c.event == HookEvent.USER_PROMPT_EXPANSION
    ]
    assert len(upe_events) == 1
    ctx = upe_events[0]
    assert ctx.expansion_source == "expand-me"
    assert "hello world" in (ctx.prompt_text or "")
    assert ctx.session_id == "sess-test"


@pytest.mark.asyncio
async def test_user_prompt_expansion_does_not_fire_for_silent_slash(captured_events):
    """A side-effect-only slash (empty output) must NOT fire — that
    isn't really a "prompt expansion"."""
    runtime = RuntimeContext(custom={"session_id": "sess-test"})
    commands = {"silent": _SilentSlash()}
    await dispatch("/silent", commands, runtime)
    await asyncio.sleep(0.05)
    upe_events = [
        c for c in captured_events if c.event == HookEvent.USER_PROMPT_EXPANSION
    ]
    assert upe_events == []


@pytest.mark.asyncio
async def test_user_prompt_expansion_does_not_fire_for_unknown_slash(captured_events):
    runtime = RuntimeContext(custom={"session_id": "sess-test"})
    await dispatch("/nope-not-real", {}, runtime)
    await asyncio.sleep(0.05)
    upe_events = [
        c for c in captured_events if c.event == HookEvent.USER_PROMPT_EXPANSION
    ]
    assert upe_events == []


# ─── INSTRUCTIONS_LOADED ─────────────────────────────────────────────────


def _make_loop_with_memory(tmp_path):
    """Construct a minimal AgentLoop with a MemoryManager pointing at
    tmp_path-scoped instruction files. Mirrors the pattern in the
    existing loop tests."""
    from opencomputer.agent.config import Config, LoopConfig
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage

    class _NoOpProvider(BaseProvider):
        async def complete(self, **kwargs):
            return ProviderResponse(
                message=Message(role="assistant", content=""),
                stop_reason="end_turn",
                usage=Usage(input_tokens=0, output_tokens=0),
            )

        async def stream_complete(self, **kwargs):
            if False:
                yield

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=tmp_path / "skills",
        user_path=tmp_path / "USER.md",
        soul_path=tmp_path / "SOUL.md",
        # Pin global SOUL.md to a non-existent tmp path so the real
        # user's ~/.opencomputer/SOUL.md doesn't leak into tests.
        global_soul_path=tmp_path / "global-soul-DOES-NOT-EXIST.md",
    )
    cfg = Config(
        loop=LoopConfig(max_iterations=1, parallel_tools=False),
        session=type(Config().session)(db_path=tmp_path / "s.db"),  # type: ignore[call-arg]
    )
    loop = AgentLoop(
        provider=_NoOpProvider(),
        config=cfg,
        db=SessionDB(tmp_path / "s.db"),
        memory=mm,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    return loop


@pytest.mark.asyncio
async def test_instructions_loaded_fires_once_per_session(tmp_path, captured_events):
    """When MEMORY.md / SOUL.md exist, INSTRUCTIONS_LOADED fires once
    per file at first turn. Repeat calls do NOT re-fire."""
    (tmp_path / "MEMORY.md").write_text("# memory rules\n- x\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("you are a careful agent\n", encoding="utf-8")
    loop = _make_loop_with_memory(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli", model="m")

    loop._emit_instructions_loaded_once(sid)
    loop._emit_instructions_loaded_once(sid)  # idempotent
    # Drain fire-and-forget tasks.
    await asyncio.sleep(0.05)

    il_events = [c for c in captured_events if c.event == HookEvent.INSTRUCTIONS_LOADED]
    paths = {c.instructions_path for c in il_events}
    assert any(p and "MEMORY.md" in p for p in paths), (
        f"expected MEMORY.md among fired paths; got {paths!r}"
    )
    assert any(p and "SOUL.md" in p for p in paths), (
        f"expected SOUL.md among fired paths; got {paths!r}"
    )
    # Idempotent: no duplicates.
    assert len(il_events) == len([p for p in paths if p])


def test_instructions_loaded_skips_missing_files(tmp_path, captured_events):
    """No MEMORY.md / SOUL.md on disk → no fires."""
    loop = _make_loop_with_memory(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli", model="m")
    loop._emit_instructions_loaded_once(sid)
    il_events = [c for c in captured_events if c.event == HookEvent.INSTRUCTIONS_LOADED]
    assert il_events == []


def test_instructions_loaded_skips_empty_files(tmp_path, captured_events):
    """A 0-byte CLAUDE.md doesn't count as 'instructions loaded'."""
    (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")
    loop = _make_loop_with_memory(tmp_path)
    sid = loop.db.allocate_session_id()
    loop.db.create_session(sid, platform="cli", model="m")
    loop._emit_instructions_loaded_once(sid)
    il_events = [c for c in captured_events if c.event == HookEvent.INSTRUCTIONS_LOADED]
    assert il_events == []


def test_instructions_loaded_empty_sid_is_noop(tmp_path, captured_events):
    (tmp_path / "MEMORY.md").write_text("# x", encoding="utf-8")
    loop = _make_loop_with_memory(tmp_path)
    loop._emit_instructions_loaded_once("")
    il_events = [c for c in captured_events if c.event == HookEvent.INSTRUCTIONS_LOADED]
    assert il_events == []


# ─── CWD_CHANGED ─────────────────────────────────────────────────────────


def test_cwd_changed_fires_on_drift(tmp_path, captured_events):
    """Two consecutive cwd polls with different values → one fire."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    loop = _make_loop_with_memory(tmp_path)
    original = os.getcwd()
    try:
        os.chdir(dir_a)
        loop._emit_cwd_changed_if_drifted()  # first call seeds, no fire
        os.chdir(dir_b)
        loop._emit_cwd_changed_if_drifted()  # second call: drift fires
    finally:
        os.chdir(original)

    cwd_events = [c for c in captured_events if c.event == HookEvent.CWD_CHANGED]
    assert len(cwd_events) == 1
    ctx = cwd_events[0]
    assert ctx.cwd is not None
    assert ctx.previous_cwd is not None
    assert ctx.cwd != ctx.previous_cwd


def test_cwd_changed_no_fire_when_unchanged(tmp_path, captured_events):
    """Polling cwd twice with no change → zero fires."""
    loop = _make_loop_with_memory(tmp_path)
    loop._emit_cwd_changed_if_drifted()
    loop._emit_cwd_changed_if_drifted()
    cwd_events = [c for c in captured_events if c.event == HookEvent.CWD_CHANGED]
    assert cwd_events == []


def test_cwd_changed_first_call_seeds_no_fire(tmp_path, captured_events):
    """First-ever poll on a fresh loop seeds the tracker, doesn't fire."""
    loop = _make_loop_with_memory(tmp_path)
    loop._emit_cwd_changed_if_drifted()
    cwd_events = [c for c in captured_events if c.event == HookEvent.CWD_CHANGED]
    assert cwd_events == []
    # ...but the tracker was seeded.
    assert loop._last_cwd != ""


# ─── POST_TOOL_BATCH ──────────────────────────────────────────────────────


# A targeted unit test for POST_TOOL_BATCH would require the full
# loop machinery; the firing site is already covered by a source-level
# guard test below. The loop integration is exercised end-to-end by
# the existing test_loop_emits_bus_events.py shape.


def test_post_tool_batch_emit_site_present_in_loop():
    """Source-level guard: a refactor of the loop must not drop the
    POST_TOOL_BATCH emission. Greps for the literal hook event."""
    src = Path(__file__).parent.parent / "opencomputer" / "agent" / "loop.py"
    text = src.read_text(encoding="utf-8")
    assert "POST_TOOL_BATCH" in text
    assert "batch_calls=" in text
    assert "batch_results=" in text


# ─── FILE_CHANGED ─────────────────────────────────────────────────────────


def test_file_changed_is_plugin_driven_documented():
    """The enum exists; no core emitter (a watcher daemon is out of
    scope for this commit). Plugins may emit it themselves via the
    standard hook engine. This test pins the contract: a plugin
    handler MUST be able to receive it when one fires."""
    captured: list[HookContext] = []

    async def _handler(ctx: HookContext) -> None:
        captured.append(ctx)

    spec = HookSpec(event=HookEvent.FILE_CHANGED, handler=_handler, fire_and_forget=True)
    hook_engine.register(spec)
    try:
        # Synthesize an emit (what a plugin watcher would do).
        async def _fire():
            hook_engine.fire_and_forget(
                HookContext(
                    event=HookEvent.FILE_CHANGED,
                    session_id="s",
                    file_path="/tmp/x.py",
                    change_kind="modified",
                )
            )

        asyncio.run(_fire())
        # Drain the fire-and-forget queue.
        asyncio.run(asyncio.sleep(0.05))
    finally:
        unregister = getattr(hook_engine, "unregister", None)
        if callable(unregister):
            try:
                unregister(spec)
            except Exception:  # noqa: BLE001
                pass
    assert len(captured) == 1
    assert captured[0].file_path == "/tmp/x.py"
    assert captured[0].change_kind == "modified"
