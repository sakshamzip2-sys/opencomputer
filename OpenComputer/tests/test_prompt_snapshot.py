"""Phase gap-closure §3.4: the system prompt is frozen per session.

Hermes's memory-tool pattern: capture `_system_prompt_snapshot` at session
load, render the system prompt from that snapshot forever after. Memory
writes during the session go to disk immediately but do not mutate the
snapshot — that invariant is what makes Anthropic's prefix cache hit on
turn 2+ instead of paying full-context dollars on every message.

These tests assert the snapshot is:
- built once per session (first turn only)
- reused verbatim on subsequent turns of the same session
- isolated per session (each session has its own snapshot)
- bypassed when the caller supplies system_override
- not invalidated by mid-session memory edits
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
    SessionConfig,
)
from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, Usage


def _config(tmp: Path) -> Config:
    return Config(
        model=ModelConfig(
            provider="mock", model="mock-model", max_tokens=1024, temperature=0.0
        ),
        loop=LoopConfig(max_iterations=2, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


def _mock_provider_endturn(reply: str = "done") -> MagicMock:
    p = MagicMock()
    p.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content=reply),
            stop_reason="end_turn",
            usage=Usage(10, 3),
        )
    )
    return p


async def test_snapshot_built_once_per_session_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)

    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    loop = AgentLoop(
        provider=_mock_provider_endturn(), config=cfg, compaction_disabled=True
    )
    # Track how many times the prompt builder is invoked — should be 1 even
    # after several turns on the same session.
    build_spy = MagicMock(wraps=loop.prompt_builder.build)
    loop.prompt_builder.build = build_spy

    await loop.run_conversation(user_message="first", session_id="s-a")
    await loop.run_conversation(user_message="second", session_id="s-a")
    await loop.run_conversation(user_message="third", session_id="s-a")

    assert build_spy.call_count == 1, (
        f"prompt builder was called {build_spy.call_count} times; "
        "snapshot should have frozen the prompt after the first turn"
    )
    # The snapshot is cached under the session id
    assert "s-a" in loop._prompt_snapshots


async def test_snapshot_is_isolated_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    loop = AgentLoop(
        provider=_mock_provider_endturn(), config=cfg, compaction_disabled=True
    )
    build_spy = MagicMock(wraps=loop.prompt_builder.build)
    loop.prompt_builder.build = build_spy

    await loop.run_conversation(user_message="hi", session_id="s-1")
    await loop.run_conversation(user_message="hi", session_id="s-2")
    await loop.run_conversation(user_message="hi", session_id="s-1")  # cached
    await loop.run_conversation(user_message="hi", session_id="s-2")  # cached

    # One build per new session, none for subsequent turns.
    assert build_spy.call_count == 2
    assert set(loop._prompt_snapshots.keys()) == {"s-1", "s-2"}


async def test_mid_session_memory_edit_does_not_invalidate_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent writing to MEMORY.md mid-session must NOT trigger a rebuild on
    the next turn — that's the invariant that keeps the prefix cache hot."""
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.memory.declarative_path.write_text("initial memory\n")
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    loop = AgentLoop(
        provider=_mock_provider_endturn(), config=cfg, compaction_disabled=True
    )

    await loop.run_conversation(user_message="turn 1", session_id="s-mem")
    first_snapshot = loop._prompt_snapshots["s-mem"]

    # Agent writes to memory between turns (simulate the skill_manage path).
    cfg.memory.declarative_path.write_text(
        "initial memory\nnew fact added by agent\n"
    )
    (cfg.memory.skills_path / "brand-new-skill").mkdir(parents=True, exist_ok=True)
    (cfg.memory.skills_path / "brand-new-skill" / "SKILL.md").write_text(
        "---\nname: brand-new-skill\ndescription: added mid-session\n---\n\nbody\n"
    )

    await loop.run_conversation(user_message="turn 2", session_id="s-mem")
    second_snapshot = loop._prompt_snapshots["s-mem"]

    assert first_snapshot == second_snapshot, (
        "snapshot changed after a mid-session memory edit — prefix cache would miss"
    )


async def test_system_override_bypasses_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    loop = AgentLoop(
        provider=_mock_provider_endturn(), config=cfg, compaction_disabled=True
    )
    build_spy = MagicMock(wraps=loop.prompt_builder.build)
    loop.prompt_builder.build = build_spy

    await loop.run_conversation(
        user_message="hi",
        session_id="s-ovr",
        system_override="custom system prompt",
    )
    # Override path must not build, must not cache
    assert build_spy.call_count == 0
    assert "s-ovr" not in loop._prompt_snapshots
