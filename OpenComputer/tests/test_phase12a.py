"""Phase 12a — Memory tool + post-response reviewer + agent cache.

Three new modules tested + their AgentLoop integration:

1. opencomputer/tools/memory.py    — MemoryTool (search / note / recall_session)
2. opencomputer/agent/agent_cache.py — LRU keyed by config_signature
3. opencomputer/agent/reviewer.py  — fire-and-forget post-response reviewer
4. opencomputer/agent/loop.py      — wires reviewer after END_TURN; reviewer
                                     spawn is suppressed when is_reviewer=True

The Tier-1 acceptance: the agent (not just the user) can now query the
episodic store mid-turn, write durable notes, and receive opportunistic
"hey, this looked worth remembering" curation in the background.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message, ToolCall


def _call(action: str, **kwargs) -> ToolCall:
    return ToolCall(id="tc-1", name="Memory", arguments={"action": action, **kwargs})


# ─── MemoryTool: schema + dispatch ─────────────────────────────────────


def test_memory_tool_schema_lists_three_actions(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    schema = tool.schema
    assert schema.name == "Memory"
    assert set(schema.parameters["properties"]["action"]["enum"]) == {
        "search",
        "note",
        "recall_session",
    }
    assert schema.parameters["required"] == ["action"]


async def test_memory_tool_unknown_action_returns_error(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("delete_everything"))
    assert res.is_error
    assert "unknown action" in res.content


# ─── MemoryTool: search ────────────────────────────────────────────────


async def test_memory_search_returns_episodic_and_message_hits(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    db.create_session("s-1", platform="cli", model="m")
    db.append_message("s-1", Message(role="user", content="how do I refactor auth"))
    db.record_episodic(
        session_id="s-1",
        turn_index=0,
        summary="discussed auth refactor approach",
        tools_used=["Edit"],
        file_paths=["src/auth.py"],
    )

    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("search", query="auth"))
    assert not res.is_error
    assert "Episodic" in res.content
    assert "discussed auth refactor" in res.content


async def test_memory_search_no_matches_returns_friendly_msg(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("search", query="xyzzy"))
    assert not res.is_error
    assert "No memory matches" in res.content


async def test_memory_search_requires_query(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("search"))
    assert res.is_error
    assert "requires query" in res.content


# ─── MemoryTool: note ──────────────────────────────────────────────────


async def test_memory_note_appends_to_memory_md(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem_path = tmp_path / "MEMORY.md"
    mem = MemoryManager(declarative_path=mem_path, skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("note", text="prefers concise responses"))
    assert not res.is_error
    assert "Noted" in res.content
    assert "prefers concise responses" in mem_path.read_text(encoding="utf-8")


async def test_memory_note_rejects_oversize(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MAX_NOTE_CHARS, MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("note", text="x" * (MAX_NOTE_CHARS + 10)))
    assert res.is_error
    assert "exceeds" in res.content


async def test_memory_note_requires_text(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("note"))
    assert res.is_error
    assert "requires text" in res.content


# ─── MemoryTool: recall_session ────────────────────────────────────────


async def test_memory_recall_session_returns_messages_truncated(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    db.create_session("abcdef1234567890", platform="cli", model="m")
    for i in range(40):
        db.append_message("abcdef1234567890", Message(role="user", content=f"msg-{i}"))

    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("recall_session", session_id="abcdef12", limit=5))
    assert not res.is_error
    # Truncated marker present
    assert "truncated" in res.content
    # Last 5 should appear; first should not
    assert "msg-39" in res.content
    assert "msg-0" not in res.content


async def test_memory_recall_session_resolves_8char_prefix(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    db.create_session("abcd1111-the-rest", platform="cli", model="m")
    db.append_message("abcd1111-the-rest", Message(role="user", content="hi"))

    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("recall_session", session_id="abcd1111"))
    assert not res.is_error
    assert "hi" in res.content


async def test_memory_recall_session_unknown_returns_error(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("recall_session", session_id="ghost000"))
    assert res.is_error
    assert "no session found" in res.content


async def test_memory_recall_session_requires_id(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.state import SessionDB
    from opencomputer.tools.memory import MemoryTool

    db = SessionDB(tmp_path / "s.db")
    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    tool = MemoryTool(db=db, memory=mem)
    res = await tool.execute(_call("recall_session"))
    assert res.is_error
    assert "requires session_id" in res.content


# ─── AgentCache ────────────────────────────────────────────────────────


def test_agent_cache_put_and_get_round_trip() -> None:
    from opencomputer.agent.agent_cache import AgentCache

    c = AgentCache(max_size=3)
    c.put(("k1",), "v1")
    assert c.get(("k1",)) == "v1"
    assert c.get(("missing",)) is None


def test_agent_cache_evicts_least_recently_used() -> None:
    from opencomputer.agent.agent_cache import AgentCache

    c = AgentCache(max_size=3)
    c.put(("a",), 1)
    c.put(("b",), 2)
    c.put(("c",), 3)
    # Touch a — now b is the LRU
    c.get(("a",))
    c.put(("d",), 4)
    assert c.get(("b",)) is None
    assert c.get(("a",)) == 1
    assert c.get(("c",)) == 3
    assert c.get(("d",)) == 4
    assert len(c) == 3


def test_agent_cache_get_or_create_caches_factory_result() -> None:
    from opencomputer.agent.agent_cache import AgentCache

    c = AgentCache(max_size=4)
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return f"built-{calls['n']}"

    assert c.get_or_create(("k",), factory) == "built-1"
    assert c.get_or_create(("k",), factory) == "built-1"  # cache hit, no rebuild
    assert calls["n"] == 1


def test_agent_cache_invalidate_drops_entry() -> None:
    from opencomputer.agent.agent_cache import AgentCache

    c = AgentCache(max_size=4)
    c.put(("x",), 99)
    c.invalidate(("x",))
    assert c.get(("x",)) is None
    # Idempotent on missing key
    c.invalidate(("never-there",))


def test_config_signature_is_order_stable() -> None:
    from opencomputer.agent.agent_cache import config_signature

    s1 = config_signature(
        provider_name="anthropic",
        model="opus",
        system_prompt_hash="abc",
        tool_names=["Read", "Write", "Bash"],
    )
    s2 = config_signature(
        provider_name="anthropic",
        model="opus",
        system_prompt_hash="abc",
        tool_names=["Bash", "Read", "Write"],
    )
    assert s1 == s2  # tool order shouldn't matter


def test_config_signature_changes_when_any_dim_changes() -> None:
    from opencomputer.agent.agent_cache import config_signature

    base = config_signature(
        provider_name="anthropic",
        model="opus",
        system_prompt_hash="abc",
        tool_names=["Read"],
    )
    # different model
    assert base != config_signature(
        provider_name="anthropic",
        model="sonnet",
        system_prompt_hash="abc",
        tool_names=["Read"],
    )
    # different prompt hash
    assert base != config_signature(
        provider_name="anthropic",
        model="opus",
        system_prompt_hash="xyz",
        tool_names=["Read"],
    )
    # different tools
    assert base != config_signature(
        provider_name="anthropic",
        model="opus",
        system_prompt_hash="abc",
        tool_names=["Read", "Write"],
    )


# ─── PostResponseReviewer ──────────────────────────────────────────────


def test_reviewer_skips_when_phrase_not_notable(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.reviewer import PostResponseReviewer

    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    rev = PostResponseReviewer(memory=mem)
    result = rev.review(user_message="what's 2+2", assistant_message="4")
    assert not result.noted
    assert result.skipped_reason == "not-notable"


def test_reviewer_notes_when_assistant_signals_remember(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.reviewer import PostResponseReviewer

    mem_path = tmp_path / "MEMORY.md"
    mem = MemoryManager(declarative_path=mem_path, skills_path=tmp_path / "skills")
    rev = PostResponseReviewer(memory=mem)
    result = rev.review(
        user_message="I prefer Python over Go for scripts",
        assistant_message="Got it, you prefer Python over Go for scripts. I'll remember.",
    )
    assert result.noted
    assert "I prefer Python over Go" in mem_path.read_text(encoding="utf-8")


def test_reviewer_dedups_consecutive_same_note(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.reviewer import PostResponseReviewer

    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    rev = PostResponseReviewer(memory=mem)
    rev.review(
        user_message="prefer terse output",
        assistant_message="Got it, you prefer terse output. I'll remember.",
    )
    second = rev.review(
        user_message="prefer terse output",
        assistant_message="Got it, you prefer terse output. I'll remember.",
    )
    assert not second.noted
    assert second.skipped_reason == "duplicate"


def test_reviewer_blocks_recursion_when_is_reviewer(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.reviewer import PostResponseReviewer

    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    rev = PostResponseReviewer(memory=mem, is_reviewer=True)
    result = rev.review(
        user_message="prefer rust",
        assistant_message="Got it, you prefer rust. I'll remember.",
    )
    assert not result.noted
    assert result.skipped_reason == "reviewer-recursion-blocked"


async def test_reviewer_spawn_is_fire_and_forget_returns_task(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager
    from opencomputer.agent.reviewer import PostResponseReviewer

    mem = MemoryManager(declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills")
    rev = PostResponseReviewer(memory=mem)
    task = rev.spawn_review(
        user_message="loves coffee",
        assistant_message="Got it, you love coffee. I'll remember.",
    )
    assert isinstance(task, asyncio.Task)
    result = await task
    assert result.noted


# ─── AgentLoop integration ────────────────────────────────────────────


def _config(tmp: Path):
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )

    return Config(
        model=ModelConfig(provider="mock", model="mock", max_tokens=512, temperature=0.0),
        loop=LoopConfig(max_iterations=2, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "s.db"),
        memory=MemoryConfig(declarative_path=tmp / "MEMORY.md", skills_path=tmp / "skills"),
    )


async def test_agent_loop_spawns_reviewer_after_end_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(
                role="assistant",
                content="Got it, you prefer X. I'll remember.",
            ),
            stop_reason="end_turn",
            usage=Usage(10, 3),
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,  # narrow this test to the reviewer
    )
    assert loop._reviewer is not None  # reviewer attached by default
    await loop.run_conversation(user_message="I prefer X", session_id="s-rev")
    # Give the spawned task a chance to run
    await asyncio.sleep(0.05)
    # The reviewer is what writes — its side effect is the strongest signal
    # that it actually fired (and didn't crash silently).
    assert "I prefer X" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


async def test_agent_loop_reviewer_disabled_skips_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(1, 1),
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    assert loop._reviewer is None
    await loop.run_conversation(user_message="hi", session_id="s-no-rev")
    # No write to MEMORY.md
    assert (
        not (tmp_path / "MEMORY.md").exists()
        or (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ""
    )


async def test_agent_loop_is_reviewer_flag_blocks_self_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop running AS the reviewer must not get its own reviewer attached."""
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(1, 1),
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        is_reviewer=True,
    )
    assert loop._reviewer is None  # recursion-guarded at construction


# ─── CLI registration ──────────────────────────────────────────────────


def test_register_builtin_tools_includes_memory() -> None:
    from opencomputer.cli import _register_builtin_tools
    from opencomputer.tools.registry import registry

    _register_builtin_tools()
    assert "Memory" in set(registry.names())
