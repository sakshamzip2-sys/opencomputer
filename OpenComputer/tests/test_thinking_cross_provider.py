"""End-to-end: a non-native provider's text stream containing
<think>...</think> tags ends up routed through thinking_callback,
identical to what an Anthropic native-thinking provider produces.

This is the key contract for "model-agnostic extended thinking".
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from opencomputer.agent.thinking_parser import ThinkingTagsParser
from plugin_sdk.provider_contract import StreamEvent


async def _fake_stream(*chunks: str) -> AsyncIterator[StreamEvent]:
    for c in chunks:
        yield StreamEvent(kind="text_delta", text=c)
    yield StreamEvent(kind="done")


def test_full_flow_non_native_provider_emits_thinking_via_parser():
    """Simulates the loop's stream-consumption path. The parser is
    inserted as a wrapper; thinking_callback receives the contents of
    <think> blocks; stream_callback receives the cleaned visible text."""
    text_chunks: list[str] = []
    thinking_chunks: list[str] = []

    async def consume():
        wrapped = ThinkingTagsParser().wrap(
            _fake_stream(
                "Sure! <think>let me ",
                "think about this</th",
                "ink> The answer is ",
                "<think>actually 41</think>42.",
            )
        )
        async for ev in wrapped:
            if ev.kind == "text_delta":
                text_chunks.append(ev.text or "")
            elif ev.kind == "thinking_delta":
                thinking_chunks.append(ev.text or "")

    asyncio.run(consume())

    text = "".join(text_chunks)
    thinking = "".join(thinking_chunks)
    # Visible text is clean, no tags.
    assert "<think>" not in text and "</think>" not in text
    assert text == "Sure!  The answer is 42."
    # Thinking captured both blocks.
    assert thinking == "let me think about thisactually 41"


def test_full_flow_with_native_provider_uses_existing_path():
    """When the provider already emits thinking_delta natively, the
    parser is NOT wired in (loop skips the wrap). Verify the parser is
    a no-op on a stream that already has thinking_delta events — they
    pass through verbatim if the parser IS wrapped (defense-in-depth)."""
    parser = ThinkingTagsParser()

    async def native_stream():
        yield StreamEvent(kind="text_delta", text="Hello ")
        yield StreamEvent(kind="thinking_delta", text="I think...")
        yield StreamEvent(kind="text_delta", text="world.")
        yield StreamEvent(kind="done")

    async def consume():
        out = []
        async for ev in parser.wrap(native_stream()):
            out.append(ev)
        return out

    out = asyncio.run(consume())
    thinking = "".join((e.text or "") for e in out if e.kind == "thinking_delta")
    text = "".join((e.text or "") for e in out if e.kind == "text_delta")
    assert thinking == "I think..."
    assert text == "Hello world."


def test_runtime_custom_flag_false_means_fallback_active():
    """Cli.py wires _provider_supports_native_thinking=False for
    gpt-4o; loop.py reads it and decides to wrap. Verify the contract
    is symmetric."""
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    rt.custom["_provider_supports_native_thinking"] = False
    rt.custom["reasoning_effort"] = "high"

    # Mirror loop.py's gating logic.
    eff = str(rt.custom.get("reasoning_effort") or "medium").lower()
    native = bool(rt.custom.get("_provider_supports_native_thinking", False))
    should_wrap = eff != "none" and not native
    assert should_wrap is True


def test_runtime_custom_flag_true_means_no_wrap():
    """Anthropic on Sonnet 4 → flag True → loop skips the wrap."""
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    rt.custom["_provider_supports_native_thinking"] = True
    rt.custom["reasoning_effort"] = "high"

    eff = str(rt.custom.get("reasoning_effort") or "medium").lower()
    native = bool(rt.custom.get("_provider_supports_native_thinking", False))
    should_wrap = eff != "none" and not native
    assert should_wrap is False


def test_effort_none_disables_fallback_even_for_non_native():
    """User opts out of thinking entirely with /reasoning none —
    the parser must NOT be wired even if the provider lacks native."""
    from plugin_sdk.runtime_context import RuntimeContext

    rt = RuntimeContext()
    rt.custom["_provider_supports_native_thinking"] = False
    rt.custom["reasoning_effort"] = "none"

    eff = str(rt.custom.get("reasoning_effort") or "medium").lower()
    native = bool(rt.custom.get("_provider_supports_native_thinking", False))
    should_wrap = eff != "none" and not native
    assert should_wrap is False
