"""M3 #1 fix — routing prompt-override MERGE instead of replace.

When a routing rule sets ``merge_with_builder: true``, the matched
template's system prompt is appended to the PromptBuilder output (skills
/ memory / SOUL stay injected) instead of wiping it. Default false
preserves the historical replace-everything behaviour.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    RoutingConfig,
    RoutingMatch,
    RoutingRule,
    SessionConfig,
)
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.routing import resolve_template_for_event
from plugin_sdk.core import Message, MessageEvent, Platform
from plugin_sdk.provider_contract import ProviderResponse, Usage

_OVERRIDE = "ROUTING-TEMPLATE-PROMPT-MARKER"


def _config(tmp: Path) -> Config:
    return Config(
        model=ModelConfig(provider="mock", model="mock-model", max_tokens=512),
        loop=LoopConfig(max_iterations=2, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md", skills_path=tmp / "skills"
        ),
    )


def _provider() -> MagicMock:
    p = MagicMock()
    p.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=Usage(10, 3),
        )
    )
    return p


def _system_text_seen(provider: MagicMock) -> str:
    """Concatenate every system-ish string the provider was called with."""
    call = provider.complete.call_args
    parts = list(call.args) + list(call.kwargs.values())
    return "\n".join(str(p) for p in parts)


# ── routing schema + resolution ──────────────────────────────────────


def test_routing_rule_accepts_merge_flag() -> None:
    rule = RoutingRule(
        match=RoutingMatch(platform="telegram"),
        agent="x",
        merge_with_builder=True,
    )
    assert rule.merge_with_builder is True
    # default stays false — existing rules unaffected
    assert RoutingRule(match=RoutingMatch(), agent="x").merge_with_builder is False


def test_resolve_template_propagates_merge_flag() -> None:
    from types import SimpleNamespace

    routing = RoutingConfig(
        rules=(
            RoutingRule(
                match=RoutingMatch(platform="telegram"),
                agent="stocks",
                merge_with_builder=True,
            ),
        )
    )
    templates = {"stocks": SimpleNamespace(name="stocks", system_prompt="be a bot")}
    event = MessageEvent(
        platform=Platform.TELEGRAM, chat_id="c", user_id="u", text="hi",
        timestamp=0.0, metadata={},
    )
    resolved = resolve_template_for_event(routing, event, templates)
    assert resolved is not None
    assert resolved.merge_with_builder is True


# ── loop behaviour ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_false_replaces_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))
    loop = AgentLoop(provider=_provider(), config=cfg, compaction_disabled=True)
    build_spy = MagicMock(wraps=loop.prompt_builder.build)
    loop.prompt_builder.build = build_spy

    await loop.run_conversation(
        user_message="hi",
        session_id="s-replace",
        system_prompt_override=_OVERRIDE,
        system_prompt_merge=False,
    )
    # replace mode — builder never runs, no snapshot cached
    assert build_spy.call_count == 0
    assert "s-replace" not in loop._prompt_snapshots
    assert _OVERRIDE in _system_text_seen(loop.provider)


@pytest.mark.asyncio
async def test_merge_true_keeps_builder_and_appends_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))
    loop = AgentLoop(provider=_provider(), config=cfg, compaction_disabled=True)
    build_spy = MagicMock(wraps=loop.prompt_builder.build)
    loop.prompt_builder.build = build_spy

    await loop.run_conversation(
        user_message="hi",
        session_id="s-merge",
        system_prompt_override=_OVERRIDE,
        system_prompt_merge=True,
    )
    # merge mode — builder DID run (skills/memory/SOUL preserved) and the
    # snapshot is cached, AND the override text is present.
    assert build_spy.call_count == 1
    assert "s-merge" in loop._prompt_snapshots
    assert _OVERRIDE in _system_text_seen(loop.provider)


@pytest.mark.asyncio
async def test_merge_is_stable_across_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turn 2 (snapshot cached) still carries the merged override."""
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))
    loop = AgentLoop(provider=_provider(), config=cfg, compaction_disabled=True)

    await loop.run_conversation(
        user_message="t1", session_id="s-2t",
        system_prompt_override=_OVERRIDE, system_prompt_merge=True,
    )
    await loop.run_conversation(
        user_message="t2", session_id="s-2t",
        system_prompt_override=_OVERRIDE, system_prompt_merge=True,
    )
    assert _OVERRIDE in _system_text_seen(loop.provider)
