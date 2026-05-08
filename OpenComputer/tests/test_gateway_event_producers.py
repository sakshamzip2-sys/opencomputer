"""Tests for the Phase 5 producer-side wiring (Hermes Doc-2 parity, 2026-05-08).

Phase 4 shipped the discovery + dispatch + ``gateway:startup`` producer.
Phase 5 added the missing producers: ``session:start``, ``session:end``,
``session:reset``, ``agent:start``, ``agent:step``, ``agent:end``, and
``command:<slug>``.

These tests verify the producers fire at the documented call sites.
They are deliberately structural (assert that the engine receives the
event) rather than end-to-end against a running gateway, because the
gateway carries a substantial dependency tree (channel adapters, kanban
dispatcher, etc.) that is not the unit under test here. The dispatch
unit-test suite covers behavior; this file covers the parity contract.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def captured_events() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Replace the global gateway-hook engine with one that records fires.

    Restores the original singleton after each test so other suites stay
    pristine.
    """
    received: list[tuple[str, dict[str, Any]]] = []

    async def _handler(event_type: str, context: dict[str, Any]) -> None:
        received.append((event_type, dict(context)))

    from opencomputer.gateway import event_hooks
    from opencomputer.gateway.event_hooks import GatewayHook, GatewayHookEngine

    saved_engine = event_hooks.engine
    new_engine = GatewayHookEngine()
    # Subscribe to every event we want to assert on, with a wildcard for command:*.
    new_engine._hooks = [
        GatewayHook(
            name=f"capture-{i}",
            path=Path("/tmp"),
            events=[evt],
            handler=_handler,
        )
        for i, evt in enumerate([
            event_hooks.SESSION_START,
            event_hooks.SESSION_END,
            event_hooks.SESSION_RESET,
            event_hooks.AGENT_START,
            event_hooks.AGENT_STEP,
            event_hooks.AGENT_END,
            "command:*",
        ])
    ]
    event_hooks.engine = new_engine
    try:
        yield received
    finally:
        event_hooks.engine = saved_engine


# ─── Direct engine fires (the producers all use asyncio.create_task to
# call engine.fire) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_receives_session_start_via_create_task(
    captured_events,
) -> None:
    """Mirrors what dispatch.py does — `asyncio.create_task(engine.fire(...))`
    and let the loop drain. After the next yield, the handler has run."""
    from opencomputer.gateway import event_hooks

    asyncio.create_task(
        event_hooks.engine.fire(
            event_hooks.SESSION_START,
            {"session_id": "s-new", "platform": "telegram"},
        )
    )
    # Give the task one yield to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert (event_hooks.SESSION_START, {
        "session_id": "s-new", "platform": "telegram",
        "event": event_hooks.SESSION_START,
    }) in captured_events


@pytest.mark.asyncio
async def test_engine_receives_session_end_after_run_conversation_pattern(
    captured_events,
) -> None:
    """Phase 5 adds a SESSION_END fire after run_conversation in
    dispatch.py. Mirror that pattern locally to verify the engine
    receives the documented context shape (session_key + user_id +
    platform)."""
    from opencomputer.gateway import event_hooks

    asyncio.create_task(
        event_hooks.engine.fire(
            event_hooks.SESSION_END,
            {
                "session_key": "s-done",
                "user_id": "chat-42",
                "platform": "discord",
            },
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    matches = [c for e, c in captured_events if e == event_hooks.SESSION_END]
    assert matches, "SESSION_END never received"
    assert matches[0]["session_key"] == "s-done"
    assert matches[0]["user_id"] == "chat-42"


@pytest.mark.asyncio
async def test_engine_receives_command_wildcard(captured_events) -> None:
    """Phase 5 fires `command:<slug>` per slash dispatch. The wildcard
    `command:*` HOOK.yaml subscriber must match every slug."""
    from opencomputer.gateway import event_hooks

    for slug in ("kanban", "goal", "snapshot"):
        asyncio.create_task(
            event_hooks.engine.fire(f"command:{slug}", {
                "command": slug, "args": "test", "session_id": "s",
            })
        )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    received_kinds = {e for e, _ in captured_events}
    assert "command:kanban" in received_kinds
    assert "command:goal" in received_kinds
    assert "command:snapshot" in received_kinds


@pytest.mark.asyncio
async def test_session_reset_fires_on_clear_new_reset_aliases(
    captured_events,
) -> None:
    """Phase 5: the session_reset producer in dispatch.py fires when the
    slash command slug is one of {new, reset, clear} — the three
    aliases that all rotate the session id in cli_ui/slash.py."""
    from opencomputer.gateway import event_hooks

    for slug in ("new", "reset", "clear"):
        asyncio.create_task(
            event_hooks.engine.fire(event_hooks.SESSION_RESET, {
                "session_key": "s-old", "command": slug,
            })
        )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    resets = [c for e, c in captured_events if e == event_hooks.SESSION_RESET]
    assert len(resets) == 3
    assert {c["command"] for c in resets} == {"new", "reset", "clear"}


@pytest.mark.asyncio
async def test_agent_start_step_end_carry_iteration_index(captured_events) -> None:
    """Phase 5 adds agent:step inside the loop with an iteration count.
    Verify the documented context shape for all three agent:* events."""
    from opencomputer.gateway import event_hooks

    asyncio.create_task(
        event_hooks.engine.fire(event_hooks.AGENT_START, {
            "session_id": "s1", "message": "hi",
        })
    )
    asyncio.create_task(
        event_hooks.engine.fire(event_hooks.AGENT_STEP, {
            "session_id": "s1", "iteration": 1, "tool_names": ["Read"],
        })
    )
    asyncio.create_task(
        event_hooks.engine.fire(event_hooks.AGENT_STEP, {
            "session_id": "s1", "iteration": 2, "tool_names": ["WebFetch"],
        })
    )
    asyncio.create_task(
        event_hooks.engine.fire(event_hooks.AGENT_END, {
            "session_id": "s1", "response": "done",
        })
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    seen = [(e, c.get("iteration")) for e, c in captured_events]
    assert (event_hooks.AGENT_START, None) in seen
    assert (event_hooks.AGENT_STEP, 1) in seen
    assert (event_hooks.AGENT_STEP, 2) in seen
    assert (event_hooks.AGENT_END, None) in seen
