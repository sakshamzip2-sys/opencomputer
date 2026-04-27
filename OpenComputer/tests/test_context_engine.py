"""Tier-A item 10 — ContextEngine ABC + registry + CompactionEngine refactor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent import context_engine_registry as registry_mod
from opencomputer.agent.compaction import CompactionEngine
from opencomputer.agent.context_engine import (
    ContextEngine,
    ContextEngineResult,
)
from plugin_sdk.core import Message

# ──────────────────────────── ABC + defaults ────────────────────────────


def test_abc_requires_should_compress_and_compress():
    """ABC enforces the two required hook points."""

    class Incomplete(ContextEngine):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_default_lifecycle_methods_are_noops():
    class Tiny(ContextEngine):
        name = "tiny"

        def should_compress(self, *, last_input_tokens):
            return False

        async def compress(self, *, messages, last_input_tokens):
            return ContextEngineResult(messages=messages)

    e = Tiny()
    asyncio.run(e.on_session_start(session_id="x", model="y", messages=[]))
    asyncio.run(e.update_from_response(prompt_tokens=10, completion_tokens=20))
    assert e.last_prompt_tokens == 10
    assert e.last_completion_tokens == 20
    asyncio.run(e.on_session_end(session_id="x"))


# ──────────────────────────── registry ────────────────────────────


def test_compressor_auto_registered():
    """The default ``compressor`` should be auto-registered at import."""
    factory = registry_mod.get("compressor")
    assert factory is not None
    assert factory is CompactionEngine


def test_register_and_get_round_trip():
    @dataclass
    class Stub(ContextEngine):
        name: str = "stub"

        def should_compress(self, *, last_input_tokens):
            return False

        async def compress(self, *, messages, last_input_tokens):
            return ContextEngineResult(messages=messages)

    registry_mod.register("stub", Stub)
    try:
        assert registry_mod.get("stub") is Stub
        assert "stub" in registry_mod.list_engines()
    finally:
        registry_mod.unregister("stub")
    assert "stub" not in registry_mod.list_engines()


def test_unknown_engine_returns_none():
    assert registry_mod.get("does-not-exist") is None
    # build() warns + returns None instead of raising.
    assert registry_mod.build("does-not-exist") is None


def test_build_passes_kwargs_through():
    captured: dict = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    registry_mod.register("kwarg-spy", factory)
    try:
        registry_mod.build("kwarg-spy", provider="P", model="M", custom=42)
        assert captured == {"provider": "P", "model": "M", "custom": 42}
    finally:
        registry_mod.unregister("kwarg-spy")


# ──────────────────────────── compaction engine implements ABC ────────────────────────────


@pytest.mark.asyncio
async def test_compaction_engine_implements_abc():
    """``CompactionEngine`` must satisfy the new ABC contract."""
    provider = MagicMock()
    engine = CompactionEngine(provider=provider, model="claude-haiku-4-5")
    assert isinstance(engine, ContextEngine)
    assert engine.name == "compressor"
    # ABC method present + delegates correctly.
    assert engine.should_compress(last_input_tokens=0) is False


@pytest.mark.asyncio
async def test_compaction_engine_compress_returns_engine_result():
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=MagicMock(
            message=MagicMock(content="summary text"),
        )
    )
    engine = CompactionEngine(provider=provider, model="claude-haiku-4-5")
    # Force-trigger compaction: high token count, many messages.
    msgs = [Message(role="user", content="hi")] + [
        Message(role="assistant", content=f"reply {i}") for i in range(50)
    ]
    result = await engine.compress(messages=msgs, last_input_tokens=300_000)
    assert isinstance(result, ContextEngineResult)
    # Either compressed or degraded — both flag did_compress True.
    assert result.did_compress
