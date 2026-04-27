"""Tier-A item 10 follow-up — AgentLoop resolves its context engine
via the registry instead of hard-coding ``CompactionEngine``."""

from __future__ import annotations

from unittest.mock import MagicMock

from opencomputer.agent import context_engine_registry as registry_mod
from opencomputer.agent.compaction import CompactionEngine
from opencomputer.agent.context_engine import ContextEngine, ContextEngineResult

# ──────────────────────────── config field ────────────────────────────


def test_loop_config_has_context_engine_default():
    from opencomputer.agent.config import LoopConfig

    cfg = LoopConfig()
    assert cfg.context_engine == "compressor"


def test_loop_config_context_engine_overridable():
    from opencomputer.agent.config import LoopConfig

    cfg = LoopConfig(context_engine="custom")
    assert cfg.context_engine == "custom"


# ──────────────────────────── registry resolution ────────────────────────────


def test_registry_resolves_compressor_to_compaction_engine():
    """The default 'compressor' name must be wired to CompactionEngine
    so the loop's engine_or_fallback chain produces the right type."""
    factory = registry_mod.get("compressor")
    assert factory is CompactionEngine


def test_unknown_engine_returns_none():
    """Loop's ``or CompactionEngine(...)`` fallback only kicks in
    when ``build`` returns ``None`` — confirm that contract."""
    assert registry_mod.build("definitely-not-registered") is None


# ──────────────────────────── alternative engine plugin ────────────────────────────


def test_custom_engine_resolves_through_registry():
    """A plugin can register a non-default engine; the loop uses it."""

    class FakeEngine(ContextEngine):
        name = "fake"

        def __init__(self, **_kwargs):
            self.constructed = True

        def should_compress(self, *, last_input_tokens):
            return False

        async def compress(self, *, messages, last_input_tokens):
            return ContextEngineResult(messages=messages)

    registry_mod.register("fake", FakeEngine)
    try:
        engine = registry_mod.build("fake", provider="P", model="M")
        assert isinstance(engine, FakeEngine)
        assert engine.constructed is True
    finally:
        registry_mod.unregister("fake")


def test_registry_passes_constructor_kwargs_through():
    """The build() helper must thread ``provider``, ``model``,
    ``disabled``, ``memory_bridge`` into the factory the same way
    AgentLoop does."""
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    registry_mod.register("kwarg-spy-2", factory)
    try:
        registry_mod.build(
            "kwarg-spy-2",
            provider="P", model="M", disabled=True, memory_bridge=None,
        )
        assert captured == {
            "provider": "P", "model": "M",
            "disabled": True, "memory_bridge": None,
        }
    finally:
        registry_mod.unregister("kwarg-spy-2")
