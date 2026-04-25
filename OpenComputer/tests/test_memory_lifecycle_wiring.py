"""PR-6 of Hermes parity: tests for system_prompt_block + on_pre_compress + on_session_end wiring."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── T2.1 collect_system_prompt_blocks ────────────────────────────────────────

@pytest.mark.asyncio
async def test_collect_system_prompt_blocks_aggregates():
    """MemoryBridge.collect_system_prompt_blocks joins blocks from all active providers."""
    from opencomputer.agent.memory_bridge import MemoryBridge

    # Build a minimal bridge backed by a no-op context (no real provider)
    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p1 = MagicMock()
    p1.provider_id = "honcho"
    p1.system_prompt_block = AsyncMock(return_value="HONCHO INSIGHT")
    p2 = MagicMock()
    p2.provider_id = "mem0"
    p2.system_prompt_block = AsyncMock(return_value="MEM0 FACT")
    # Inject providers via the instance override path used by _iter_active_providers
    bridge._registered_providers = [p1, p2]

    result = await bridge.collect_system_prompt_blocks(session_id="s1")
    assert "HONCHO INSIGHT" in result
    assert "MEM0 FACT" in result
    assert "### From honcho" in result
    assert "### From mem0" in result


@pytest.mark.asyncio
async def test_collect_system_prompt_blocks_isolates_failure():
    """One provider's exception doesn't break the others."""
    from opencomputer.agent.memory_bridge import MemoryBridge

    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p1 = MagicMock()
    p1.provider_id = "broken"
    p1.system_prompt_block = AsyncMock(side_effect=RuntimeError("boom"))
    p2 = MagicMock()
    p2.provider_id = "good"
    p2.system_prompt_block = AsyncMock(return_value="STILL HERE")
    bridge._registered_providers = [p1, p2]

    result = await bridge.collect_system_prompt_blocks(session_id="s1")
    assert "STILL HERE" in result
    # broken provider's id should not appear in the joined output
    assert "broken" not in result


@pytest.mark.asyncio
async def test_collect_system_prompt_blocks_truncates_at_cap():
    """Per-block content is truncated to max_per_block chars."""
    from opencomputer.agent.memory_bridge import MemoryBridge

    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p = MagicMock()
    p.provider_id = "verbose"
    p.system_prompt_block = AsyncMock(return_value="x" * 5000)
    bridge._registered_providers = [p]

    result = await bridge.collect_system_prompt_blocks(session_id="s1", max_per_block=100)
    assert "…[truncated]" in result
    # Header + 100 chars + marker should be well under 500
    assert len(result) < 500


@pytest.mark.asyncio
async def test_collect_system_prompt_blocks_returns_empty_when_no_content():
    """Returns '' when all providers return None or empty."""
    from opencomputer.agent.memory_bridge import MemoryBridge

    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p = MagicMock()
    p.provider_id = "silent"
    p.system_prompt_block = AsyncMock(return_value=None)
    bridge._registered_providers = [p]

    result = await bridge.collect_system_prompt_blocks(session_id="s1")
    assert result == ""


# ─── T2.2 collect_pre_compress ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_collect_pre_compress_aggregates():
    from opencomputer.agent.memory_bridge import MemoryBridge

    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p = MagicMock()
    p.provider_id = "honcho"
    p.on_pre_compress = AsyncMock(return_value="KEY FACT FROM SESSION")
    bridge._registered_providers = [p]

    result = await bridge.collect_pre_compress([])
    assert "KEY FACT FROM SESSION" in result


@pytest.mark.asyncio
async def test_collect_pre_compress_isolates_failure():
    """A failing on_pre_compress doesn't prevent other providers from running."""
    from opencomputer.agent.memory_bridge import MemoryBridge

    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p1 = MagicMock()
    p1.provider_id = "bad"
    p1.on_pre_compress = AsyncMock(side_effect=RuntimeError("oops"))
    p2 = MagicMock()
    p2.provider_id = "ok"
    p2.on_pre_compress = AsyncMock(return_value="GOOD FACT")
    bridge._registered_providers = [p1, p2]

    result = await bridge.collect_pre_compress([])
    assert "GOOD FACT" in result


# ─── T2.3 fire_session_end ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fire_session_end_calls_each_provider():
    from opencomputer.agent.memory_bridge import MemoryBridge

    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p1 = MagicMock()
    p1.provider_id = "p1"
    p1.on_session_end = AsyncMock()
    p2 = MagicMock()
    p2.provider_id = "p2"
    p2.on_session_end = AsyncMock()
    bridge._registered_providers = [p1, p2]

    await bridge.fire_session_end("session-xyz")
    p1.on_session_end.assert_awaited_once_with("session-xyz")
    p2.on_session_end.assert_awaited_once_with("session-xyz")


@pytest.mark.asyncio
async def test_fire_session_end_isolates_failure():
    """A failing on_session_end doesn't prevent other providers from being called."""
    from opencomputer.agent.memory_bridge import MemoryBridge

    ctx = MagicMock()
    ctx.provider = None
    ctx._failure_state = {}
    bridge = MemoryBridge(ctx)

    p1 = MagicMock()
    p1.provider_id = "bad"
    p1.on_session_end = AsyncMock(side_effect=RuntimeError("fire"))
    p2 = MagicMock()
    p2.provider_id = "good"
    p2.on_session_end = AsyncMock()
    bridge._registered_providers = [p1, p2]

    # Should not raise
    await bridge.fire_session_end("session-abc")
    p2.on_session_end.assert_awaited_once_with("session-abc")


# ─── MemoryConfig feature flags ───────────────────────────────────────────────

def test_memory_config_has_enable_ambient_blocks():
    from opencomputer.agent.config import MemoryConfig

    cfg = MemoryConfig()
    assert cfg.enable_ambient_blocks is True
    assert cfg.max_ambient_block_chars == 800


def test_memory_config_enable_ambient_blocks_can_be_disabled():
    from opencomputer.agent.config import MemoryConfig

    cfg = MemoryConfig(enable_ambient_blocks=False)
    assert cfg.enable_ambient_blocks is False


# ─── ABC default no-ops ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_memory_provider_abc_default_system_prompt_block():
    """Default system_prompt_block returns None (no-op)."""
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.memory import MemoryProvider
    from plugin_sdk.tool_contract import ToolSchema

    # Minimal concrete subclass with only the abstract methods implemented
    class _Stub(MemoryProvider):
        @property
        def provider_id(self) -> str:
            return "stub"

        def tool_schemas(self) -> list[ToolSchema]:
            return []

        async def handle_tool_call(self, call: ToolCall) -> ToolResult:
            return ToolResult(tool_call_id=call.id, content="", is_error=False)

        async def prefetch(self, query: str, turn_index: int) -> str | None:
            return None

        async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
            return None

        async def health_check(self) -> bool:
            return True

    stub = _Stub()
    result = await stub.system_prompt_block(session_id="s1")
    assert result is None


@pytest.mark.asyncio
async def test_memory_provider_abc_default_on_pre_compress():
    """Default on_pre_compress returns None (no-op)."""
    from plugin_sdk.core import ToolCall, ToolResult
    from plugin_sdk.memory import MemoryProvider
    from plugin_sdk.tool_contract import ToolSchema

    class _Stub(MemoryProvider):
        @property
        def provider_id(self) -> str:
            return "stub"

        def tool_schemas(self) -> list[ToolSchema]:
            return []

        async def handle_tool_call(self, call: ToolCall) -> ToolResult:
            return ToolResult(tool_call_id=call.id, content="", is_error=False)

        async def prefetch(self, query: str, turn_index: int) -> str | None:
            return None

        async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
            return None

        async def health_check(self) -> bool:
            return True

    stub = _Stub()
    result = await stub.on_pre_compress([])
    assert result is None


# ─── prompt_builder.build_with_memory ────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_with_memory_appends_memory_context():
    """build_with_memory appends ## Memory context when bridge returns blocks."""
    from opencomputer.agent.prompt_builder import PromptBuilder

    pb = PromptBuilder()
    bridge = MagicMock()
    bridge.collect_system_prompt_blocks = AsyncMock(return_value="### From honcho\n\nSome memory")

    result = await pb.build_with_memory(
        memory_bridge=bridge,
        session_id="s1",
        enable_ambient_blocks=True,
        max_ambient_block_chars=800,
    )
    assert "## Memory context" in result
    assert "Some memory" in result


@pytest.mark.asyncio
async def test_build_with_memory_skips_when_disabled():
    """build_with_memory skips the ## Memory context block when flag is False."""
    from opencomputer.agent.prompt_builder import PromptBuilder

    pb = PromptBuilder()
    bridge = MagicMock()
    bridge.collect_system_prompt_blocks = AsyncMock(return_value="SHOULD NOT APPEAR")

    result = await pb.build_with_memory(
        memory_bridge=bridge,
        session_id="s1",
        enable_ambient_blocks=False,
    )
    assert "## Memory context" not in result
    assert "SHOULD NOT APPEAR" not in result
    bridge.collect_system_prompt_blocks.assert_not_called()


@pytest.mark.asyncio
async def test_build_with_memory_skips_when_no_bridge():
    """build_with_memory is a no-op when no bridge is passed."""
    from opencomputer.agent.prompt_builder import PromptBuilder

    pb = PromptBuilder()
    result = await pb.build_with_memory(
        memory_bridge=None,
        session_id="s1",
        enable_ambient_blocks=True,
    )
    assert "## Memory context" not in result


@pytest.mark.asyncio
async def test_build_with_memory_skips_when_blocks_empty():
    """build_with_memory omits the header when bridge returns empty string."""
    from opencomputer.agent.prompt_builder import PromptBuilder

    pb = PromptBuilder()
    bridge = MagicMock()
    bridge.collect_system_prompt_blocks = AsyncMock(return_value="")

    result = await pb.build_with_memory(
        memory_bridge=bridge,
        session_id="s1",
        enable_ambient_blocks=True,
    )
    assert "## Memory context" not in result


# ─── compaction key-facts injection ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_compaction_prepends_key_facts_when_bridge_present():
    """CompactionEngine.maybe_run wraps provider key facts in DO-NOT-SUMMARIZE markers."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from opencomputer.agent.compaction import CompactionConfig, CompactionEngine

    # Minimal provider mock: complete() returns a Message with summary text
    from plugin_sdk.core import Message

    fake_resp = MagicMock()
    fake_resp.message = Message(role="assistant", content="summary from LLM")
    fake_resp.usage.input_tokens = 100
    fake_resp.usage.output_tokens = 50

    provider = MagicMock()
    provider.complete = AsyncMock(return_value=fake_resp)

    bridge = MagicMock()
    bridge.collect_pre_compress = AsyncMock(return_value="CRITICAL FACT")

    engine = CompactionEngine(
        provider=provider,
        model="claude-sonnet-4-6",
        config=CompactionConfig(
            preserve_recent=2,
            threshold_ratio=0.0,   # always fire
            summarize_max_tokens=64,
            summarize_timeout_s=5.0,
        ),
        memory_bridge=bridge,
    )

    # Build a message history big enough to trigger compaction
    msgs = [Message(role="user", content=f"msg {i}") for i in range(6)]

    result = await engine.maybe_run(msgs, last_input_tokens=999_999)
    assert result.did_compact
    # The synthetic summary message should contain the DO-NOT-SUMMARIZE wrapper
    assert "<KEY-FACTS-DO-NOT-SUMMARIZE>" in result.messages[0].content
    assert "CRITICAL FACT" in result.messages[0].content


# ─── AgentLoop.aclose ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aclose_fires_session_end(tmp_path):
    """AgentLoop.aclose() calls memory_bridge.fire_session_end."""
    from opencomputer.agent.config import Config, ModelConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop

    cfg = Config(
        model=ModelConfig(model="claude-sonnet-4-6"),
        session=SessionConfig(db_path=tmp_path / "test.db"),
    )

    provider = MagicMock()
    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )

    # Monkey-patch fire_session_end so we can assert it was called
    loop.memory_bridge.fire_session_end = AsyncMock()

    loop._current_session_id = "test-session-42"
    await loop.aclose()

    loop.memory_bridge.fire_session_end.assert_awaited_once_with("test-session-42")


@pytest.mark.asyncio
async def test_aclose_explicit_session_id(tmp_path):
    """AgentLoop.aclose(session_id=...) uses the explicit id, not _current_session_id."""
    from opencomputer.agent.config import Config, ModelConfig, SessionConfig
    from opencomputer.agent.loop import AgentLoop

    cfg = Config(
        model=ModelConfig(model="claude-sonnet-4-6"),
        session=SessionConfig(db_path=tmp_path / "test.db"),
    )

    provider = MagicMock()
    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )

    loop.memory_bridge.fire_session_end = AsyncMock()
    loop._current_session_id = "should-not-use-this"

    await loop.aclose(session_id="explicit-session-99")
    loop.memory_bridge.fire_session_end.assert_awaited_once_with("explicit-session-99")
