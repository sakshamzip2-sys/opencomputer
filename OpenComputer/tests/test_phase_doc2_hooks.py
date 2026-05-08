"""Tests — 2026-05-08 Hermes Doc-2 parity hook events.

Three new :class:`plugin_sdk.hooks.HookEvent` values:

* ``SESSION_FINALIZE`` — fires once on surface tear-down (CLI exit, etc.),
  distinct from per-turn ``SESSION_END``.
* ``SESSION_RESET`` — fires after ``/clear`` / ``/new`` / ``/reset``
  rotates a session id; previous id exposed via
  ``HookContext.previous_session_id``.
* ``TRANSFORM_LLM_OUTPUT`` — fires once per turn after the final response
  is assembled, before delivery; handlers may rewrite the response by
  returning ``HookDecision(decision="rewrite", rewritten_text=...)``.

These tests exercise the SDK shape + the public fire-helpers in
:mod:`opencomputer.hooks.session_lifecycle`. The ``TRANSFORM_LLM_OUTPUT``
agent-loop integration path is exercised separately via
``test_phase_doc2_transform_llm_output.py``-style integration tests when
infrastructure is available; here we cover the contract only.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opencomputer.hooks.engine import HookEngine
from opencomputer.hooks.session_lifecycle import (
    fire_session_finalize,
    fire_session_reset,
)
from plugin_sdk.hooks import (
    ALL_HOOK_EVENTS,
    HookContext,
    HookDecision,
    HookEvent,
    HookSpec,
)


def test_new_hook_events_in_enum() -> None:
    """Three new values are reachable via ``HookEvent.X``."""
    assert HookEvent.SESSION_FINALIZE.value == "SessionFinalize"
    assert HookEvent.SESSION_RESET.value == "SessionReset"
    assert HookEvent.TRANSFORM_LLM_OUTPUT.value == "TransformLlmOutput"


def test_new_hook_events_in_all_tuple() -> None:
    """``ALL_HOOK_EVENTS`` is exhaustive — used by audit-log plugins."""
    assert HookEvent.SESSION_FINALIZE in ALL_HOOK_EVENTS
    assert HookEvent.SESSION_RESET in ALL_HOOK_EVENTS
    assert HookEvent.TRANSFORM_LLM_OUTPUT in ALL_HOOK_EVENTS


def test_hook_context_carries_finalize_fields() -> None:
    """``HookContext`` exposes ``finalize_reason`` + ``surface_origin``."""
    ctx = HookContext(
        event=HookEvent.SESSION_FINALIZE,
        session_id="s1",
        finalize_reason="cli_exit",
        surface_origin="cli",
    )
    assert ctx.finalize_reason == "cli_exit"
    assert ctx.surface_origin == "cli"


def test_hook_context_carries_reset_fields() -> None:
    ctx = HookContext(
        event=HookEvent.SESSION_RESET,
        session_id="new-id",
        previous_session_id="old-id",
        surface_origin="cli",
    )
    assert ctx.previous_session_id == "old-id"
    assert ctx.session_id == "new-id"


def test_hook_context_carries_response_text() -> None:
    ctx = HookContext(
        event=HookEvent.TRANSFORM_LLM_OUTPUT,
        session_id="s1",
        response_text="Hello world",
        model="claude-sonnet-4-6",
    )
    assert ctx.response_text == "Hello world"
    assert ctx.model == "claude-sonnet-4-6"


def test_fire_session_finalize_invokes_handler() -> None:
    """The helper drives the registered handler through the engine."""
    received: list[HookContext] = []

    async def _handler(ctx: HookContext) -> HookDecision | None:
        received.append(ctx)
        return None

    engine = HookEngine()
    engine.register(
        HookSpec(event=HookEvent.SESSION_FINALIZE, handler=_handler)
    )

    # The helper imports `engine` from opencomputer.hooks.engine — patch
    # that module's `engine` symbol so the helper hits our test engine.
    import opencomputer.hooks.engine as engine_mod

    saved = engine_mod.engine
    engine_mod.engine = engine
    try:
        # Drive an event loop so the fire-and-forget task can complete.
        async def _run() -> None:
            fire_session_finalize(
                session_id="sess-X", reason="cli_exit", surface="cli",
            )
            # fire-and-forget schedules; await one yield so the task runs.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_run())
    finally:
        engine_mod.engine = saved

    assert len(received) == 1
    assert received[0].finalize_reason == "cli_exit"
    assert received[0].surface_origin == "cli"
    assert received[0].session_id == "sess-X"


def test_fire_session_reset_invokes_handler_with_previous_id() -> None:
    received: list[HookContext] = []

    async def _handler(ctx: HookContext) -> HookDecision | None:
        received.append(ctx)
        return None

    engine = HookEngine()
    engine.register(
        HookSpec(event=HookEvent.SESSION_RESET, handler=_handler)
    )

    import opencomputer.hooks.engine as engine_mod

    saved = engine_mod.engine
    engine_mod.engine = engine
    try:
        async def _run() -> None:
            fire_session_reset(
                new_session_id="new-XYZ",
                previous_session_id="old-ABC",
                surface="cli",
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_run())
    finally:
        engine_mod.engine = saved

    assert len(received) == 1
    assert received[0].previous_session_id == "old-ABC"
    assert received[0].session_id == "new-XYZ"


def test_fire_session_finalize_unknown_reason_warns_but_fires(caplog) -> None:
    """Free-form reasons are tolerated but warned — handlers still receive them."""
    received: list[HookContext] = []

    async def _handler(ctx: HookContext) -> HookDecision | None:
        received.append(ctx)
        return None

    engine = HookEngine()
    engine.register(
        HookSpec(event=HookEvent.SESSION_FINALIZE, handler=_handler)
    )

    import opencomputer.hooks.engine as engine_mod

    saved = engine_mod.engine
    engine_mod.engine = engine
    try:
        async def _run() -> None:
            fire_session_finalize(
                session_id="s1",
                reason="custom_reason_not_in_set",
                surface="wire",
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        with caplog.at_level("WARNING"):
            asyncio.run(_run())
    finally:
        engine_mod.engine = saved

    assert len(received) == 1
    assert received[0].finalize_reason == "custom_reason_not_in_set"
    assert any(
        "unrecognized reason" in r.message
        for r in caplog.records
    )


def test_transform_llm_output_decision_shape() -> None:
    """A handler can return a rewrite decision whose ``rewritten_text`` is
    consumed by callers — exercises the contract without driving the loop."""
    decision = HookDecision(
        decision="rewrite",
        rewritten_text="REDACTED: alice@example.com -> alice@***",
    )
    assert decision.decision == "rewrite"
    assert decision.rewritten_text is not None
    assert decision.rewritten_text.startswith("REDACTED")


@pytest.mark.asyncio
async def test_transform_llm_output_engine_blocking_returns_first_rewrite() -> None:
    """``fire_blocking`` returns the first non-pass decision — the agent
    loop relies on this to apply the rewrite.

    Two handlers register; the first returns ``rewrite``, the second
    would return ``pass`` but is never reached. Verifies the
    short-circuit semantics that ``opencomputer.agent.loop`` depends on.
    """
    calls: list[str] = []

    async def _handler_one(ctx: HookContext) -> HookDecision | None:
        calls.append("one")
        return HookDecision(decision="rewrite", rewritten_text="rewritten!")

    async def _handler_two(ctx: HookContext) -> HookDecision | None:
        calls.append("two")
        return HookDecision(decision="pass")

    engine = HookEngine()
    engine.register(HookSpec(
        event=HookEvent.TRANSFORM_LLM_OUTPUT, handler=_handler_one,
        priority=10,
    ))
    engine.register(HookSpec(
        event=HookEvent.TRANSFORM_LLM_OUTPUT, handler=_handler_two,
        priority=20,
    ))

    decision = await engine.fire_blocking(
        HookContext(
            event=HookEvent.TRANSFORM_LLM_OUTPUT,
            session_id="s1",
            response_text="original",
        )
    )
    assert decision is not None
    assert decision.decision == "rewrite"
    assert decision.rewritten_text == "rewritten!"
    assert calls == ["one"]  # second handler never called
