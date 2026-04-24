"""Phase 12b.6 — Cheap-route gating (Sub-project D, Task D6).

Routes short/simple first-turn user prompts to a user-configurable cheap
model. Saves cost on "what time is it" / "capital of France" / "how do
you spell X" queries without compromising harder prompts.

Two concerns tested:

1. The pure heuristic ``should_route_cheap`` in
   ``opencomputer/agent/cheap_route.py`` — 7 unit tests.
2. AgentLoop integration — 3 tests verifying which model is passed to
   ``provider.complete`` on iteration 0 vs later iterations, and the
   feature-disabled path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message, ToolCall

# ─── 1. Heuristic unit tests ───────────────────────────────────────────


def test_cheap_route_short_plain_prompt_returns_true() -> None:
    from opencomputer.agent.cheap_route import should_route_cheap

    assert should_route_cheap("what time is it?") is True


def test_cheap_route_long_prompt_returns_false() -> None:
    from opencomputer.agent.cheap_route import should_route_cheap

    # 201 chars, no disqualifying keywords
    msg = "a" * 201
    assert should_route_cheap(msg) is False


def test_cheap_route_prompt_with_url_returns_false() -> None:
    from opencomputer.agent.cheap_route import should_route_cheap

    assert should_route_cheap("look at https://example.com") is False


def test_cheap_route_prompt_with_code_fence_returns_false() -> None:
    from opencomputer.agent.cheap_route import should_route_cheap

    assert should_route_cheap("```python\nprint(1)\n```") is False


def test_cheap_route_prompt_with_disqualifying_keyword_returns_false() -> None:
    from opencomputer.agent.cheap_route import should_route_cheap

    assert should_route_cheap("write me code") is False


def test_cheap_route_keyword_as_substring_does_not_trigger() -> None:
    """'fix' is a disqualifying keyword, but 'prefix' should NOT match —
    the heuristic uses \\b word boundaries."""
    from opencomputer.agent.cheap_route import should_route_cheap

    assert should_route_cheap("the prefix is blue") is True


def test_cheap_route_respects_custom_max_chars() -> None:
    from opencomputer.agent.cheap_route import should_route_cheap

    msg = "a" * 50
    assert should_route_cheap(msg, max_chars=100) is True
    assert should_route_cheap(msg, max_chars=10) is False


# ─── 2. AgentLoop integration ──────────────────────────────────────────


def _config(tmp: Path, *, cheap_model: str | None = None):
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )

    return Config(
        model=ModelConfig(
            provider="mock",
            model="main-model",
            max_tokens=512,
            temperature=0.0,
            cheap_model=cheap_model,
        ),
        loop=LoopConfig(max_iterations=3, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "s.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


async def test_agent_loop_uses_cheap_model_when_heuristic_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Short, simple, feature-enabled → cheap model used on first turn."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path, cheap_model="haiku-model")
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="42"),
            stop_reason="end_turn",
            usage=Usage(5, 2),
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    await loop.run_conversation(user_message="what time is it?", session_id="s-cheap")

    assert provider.complete.await_count == 1
    kwargs = provider.complete.await_args_list[0].kwargs
    assert kwargs["model"] == "haiku-model"


async def test_agent_loop_uses_main_model_when_cheap_route_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cheap_model=None → main model used, even on short-simple prompt."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path, cheap_model=None)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="42"),
            stop_reason="end_turn",
            usage=Usage(5, 2),
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    await loop.run_conversation(
        user_message="what time is it?", session_id="s-disabled"
    )

    assert provider.complete.await_count == 1
    kwargs = provider.complete.await_args_list[0].kwargs
    assert kwargs["model"] == "main-model"


async def test_agent_loop_does_not_route_cheap_on_second_iter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Iteration 0 may use cheap; iteration 1+ MUST use main model.

    Simulates: turn 0 returns a tool_use (forces iteration 1); turn 1
    returns end_turn. Verify both calls' `model` kwargs.
    """
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry
    from plugin_sdk.core import ToolResult
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path, cheap_model="haiku-model")
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    # Patch the registry dispatch so the synthetic tool_use doesn't need
    # a real tool registered.
    async def _fake_dispatch(call: ToolCall, **_: object) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id, content="ok", is_error=False
        )

    monkeypatch.setattr(registry, "dispatch", _fake_dispatch)

    # Turn 0: tool_use. Turn 1: end_turn.
    turn0 = ProviderResponse(
        message=Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tc-1", name="Bash", arguments={"cmd": "date"})],
        ),
        stop_reason="tool_use",
        usage=Usage(5, 2),
    )
    turn1 = ProviderResponse(
        message=Message(role="assistant", content="done"),
        stop_reason="end_turn",
        usage=Usage(5, 2),
    )

    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[turn0, turn1])

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    await loop.run_conversation(user_message="quick q", session_id="s-iter")

    assert provider.complete.await_count == 2
    kw0 = provider.complete.await_args_list[0].kwargs
    kw1 = provider.complete.await_args_list[1].kwargs
    # First turn: cheap routing active (prompt is short + keyword-free)
    assert kw0["model"] == "haiku-model"
    # Second turn: must NOT be cheap-routed
    assert kw1["model"] == "main-model"
