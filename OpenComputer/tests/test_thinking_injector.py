"""ThinkingInjector activates a <think>-tag instruction in the system
prompt only when (a) reasoning effort is set, and (b) the active
provider lacks native thinking support."""
from __future__ import annotations

import asyncio

from opencomputer.agent.thinking_injector import ThinkingInjector
from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import RuntimeContext


def _ctx(*, effort=None, native=None) -> InjectionContext:
    rt = RuntimeContext()
    if effort is not None:
        rt.custom["reasoning_effort"] = effort
    if native is not None:
        rt.custom["_provider_supports_native_thinking"] = native
    return InjectionContext(messages=(), runtime=rt)


def test_provider_id_is_stable():
    injector = ThinkingInjector()
    assert injector.provider_id == "thinking_tags_fallback"


def test_priority_runs_after_plan_yolo_modes():
    """plan=10, yolo=20; thinking should land later so primary modes win."""
    injector = ThinkingInjector()
    assert injector.priority >= 50


def test_returns_none_when_native_thinking_supported():
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort="high", native=True)))
    assert out is None


def test_returns_none_when_effort_is_none():
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort="none", native=False)))
    assert out is None


def test_returns_none_when_effort_unset_but_native_true():
    """If no effort is set BUT provider has native thinking, the
    injector still skips — native API handles it."""
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort=None, native=True)))
    assert out is None


def test_returns_instruction_when_effort_unset_and_native_false():
    """Effort defaults to 'medium' per reasoning_cmd._DEFAULT_LEVEL —
    when not explicitly set, the injector still kicks in for non-
    native providers because the default IS effective use."""
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort=None, native=False)))
    assert out is not None
    assert "<think>" in out


def test_returns_instruction_when_native_false_and_effort_set():
    injector = ThinkingInjector()
    out = asyncio.run(injector.collect(_ctx(effort="high", native=False)))
    assert out is not None
    assert "<think>" in out and "</think>" in out
    # Mentions the contract clearly.
    assert "reasoning" in out.lower()


def test_default_provider_assumption_is_no_native_support():
    """If the runtime hasn't been told about provider capabilities
    (e.g. wire path before cli.py wires it), default to assuming NO
    native support — model-agnostic fallback is the safer default."""
    injector = ThinkingInjector()
    rt = RuntimeContext()
    rt.custom["reasoning_effort"] = "medium"
    # NOTE: NOT setting _provider_supports_native_thinking.
    out = asyncio.run(
        injector.collect(InjectionContext(messages=(), runtime=rt))
    )
    assert out is not None
