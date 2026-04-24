"""II.6 — reasoning-chain metadata persistence.

Providers that expose reasoning (Anthropic extended thinking, OpenAI o1 /
o3 family, Nous, OpenRouter unified-reasoning responses) return the
reasoning text + structured details on ``ProviderResponse``. Those
fields must flow into SessionDB so the assistant message's reasoning
chain survives a session reload — otherwise multi-turn reasoning
continuity breaks for reasoning-model workloads.

This module tests:
  * ``ProviderResponse`` has optional reasoning fields that default to
    ``None`` (backwards-compatible for providers that don't set them).
  * ``SessionDB.append_message`` accepts reasoning_details +
    codex_reasoning_items and stores them; ``get_messages`` round-trips
    them. The existing ``reasoning`` column already round-trips via
    ``Message.reasoning``.
  * ``AgentLoop._run_one_step`` copies reasoning fields from
    ``ProviderResponse`` onto ``step.assistant_message`` so the SQLite
    write captures them.
  * End-to-end: a mock provider returning reasoning text →
    ``run_conversation`` → SessionDB row has reasoning populated.
  * End-to-end: a mock provider returning NO reasoning → SessionDB
    row has NULL reasoning (no crash, backwards compat).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opencomputer.agent.config import Config, LoopConfig, MemoryConfig, ModelConfig, SessionConfig
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, Usage

# ─── helpers ───────────────────────────────────────────────────────────


def _config(tmp_path: Path) -> Config:
    return Config(
        model=ModelConfig(provider="anthropic", model="claude-opus-4-7"),
        loop=LoopConfig(max_iterations=4, parallel_tools=False),
        session=SessionConfig(db_path=tmp_path / ".opencomputer" / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp_path / ".opencomputer" / "MEMORY.md",
            skills_path=tmp_path / ".opencomputer" / "skills",
            user_path=tmp_path / ".opencomputer" / "USER.md",
            soul_path=tmp_path / ".opencomputer" / "SOUL.md",
        ),
    )


# ─── ProviderResponse SDK surface ──────────────────────────────────────


def test_provider_response_reasoning_fields_default_none() -> None:
    """Providers that don't set reasoning still work — fields default to None."""
    resp = ProviderResponse(
        message=Message(role="assistant", content="hi"),
        stop_reason="end_turn",
        usage=Usage(1, 1),
    )
    assert resp.reasoning is None
    assert resp.reasoning_details is None
    assert resp.codex_reasoning_items is None


def test_provider_response_accepts_reasoning_fields() -> None:
    """Providers CAN set reasoning text and structured details."""
    resp = ProviderResponse(
        message=Message(role="assistant", content="hi"),
        stop_reason="end_turn",
        usage=Usage(1, 1),
        reasoning="let me think step by step",
        reasoning_details=[{"type": "reasoning.text", "text": "…"}],
        codex_reasoning_items=[{"type": "reasoning", "summary": [{"type": "text", "text": "…"}]}],
    )
    assert resp.reasoning == "let me think step by step"
    assert resp.reasoning_details == [{"type": "reasoning.text", "text": "…"}]
    assert resp.codex_reasoning_items[0]["type"] == "reasoning"


# ─── Message SDK surface ───────────────────────────────────────────────


def test_message_reasoning_details_default_none() -> None:
    msg = Message(role="assistant", content="hi")
    assert msg.reasoning is None
    assert msg.reasoning_details is None
    assert msg.codex_reasoning_items is None


def test_message_accepts_reasoning_details() -> None:
    msg = Message(
        role="assistant",
        content="hi",
        reasoning="thinking",
        reasoning_details=[{"type": "reasoning.text", "text": "…"}],
        codex_reasoning_items=[{"type": "reasoning"}],
    )
    assert msg.reasoning == "thinking"
    assert msg.reasoning_details[0]["type"] == "reasoning.text"
    assert msg.codex_reasoning_items[0]["type"] == "reasoning"


# ─── SessionDB persistence ─────────────────────────────────────────────


def test_session_db_round_trips_reasoning_text(tmp_path: Path) -> None:
    """``reasoning`` text on a Message round-trips through SQLite."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1")
    msg = Message(
        role="assistant",
        content="answer",
        reasoning="let me think about this carefully",
    )
    db.append_message("s-1", msg)
    got = db.get_messages("s-1")
    assert len(got) == 1
    assert got[0].reasoning == "let me think about this carefully"


def test_session_db_round_trips_reasoning_details(tmp_path: Path) -> None:
    """``reasoning_details`` (structured) round-trips as JSON."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1")
    details = [
        {"type": "reasoning.text", "text": "consider option A"},
        {"type": "reasoning.text", "text": "consider option B"},
    ]
    msg = Message(
        role="assistant",
        content="answer",
        reasoning_details=details,
    )
    db.append_message("s-1", msg)
    got = db.get_messages("s-1")
    assert len(got) == 1
    assert got[0].reasoning_details == details


def test_session_db_round_trips_codex_reasoning_items(tmp_path: Path) -> None:
    """``codex_reasoning_items`` round-trips as JSON."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1")
    items = [
        {"type": "reasoning", "summary": [{"type": "text", "text": "planning"}]},
    ]
    msg = Message(
        role="assistant",
        content="answer",
        codex_reasoning_items=items,
    )
    db.append_message("s-1", msg)
    got = db.get_messages("s-1")
    assert len(got) == 1
    assert got[0].codex_reasoning_items == items


def test_session_db_null_reasoning_stored_as_null(tmp_path: Path) -> None:
    """No-reasoning messages write NULL columns (backwards compat)."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1")
    msg = Message(role="assistant", content="no reasoning here")
    db.append_message("s-1", msg)
    got = db.get_messages("s-1")
    assert len(got) == 1
    assert got[0].reasoning is None
    assert got[0].reasoning_details is None
    assert got[0].codex_reasoning_items is None


def test_session_db_batch_append_persists_reasoning(tmp_path: Path) -> None:
    """``append_messages_batch`` writes reasoning fields atomically."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1")
    messages = [
        Message(
            role="assistant",
            content="step 1",
            reasoning="first thought",
            reasoning_details=[{"type": "reasoning.text", "text": "a"}],
        ),
        Message(
            role="tool",
            content="tool output",
            tool_call_id="call_1",
            name="ToolX",
        ),
    ]
    db.append_messages_batch("s-1", messages)
    got = db.get_messages("s-1")
    assert len(got) == 2
    assert got[0].reasoning == "first thought"
    assert got[0].reasoning_details == [{"type": "reasoning.text", "text": "a"}]
    assert got[1].reasoning is None


# ─── AgentLoop integration ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_loop_persists_provider_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: provider returns reasoning → SessionDB row captures it."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    reasoning_text = "I should approach this step by step…"
    reasoning_details = [{"type": "reasoning.text", "text": "deep thought"}]

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="here is the answer"),
            stop_reason="end_turn",
            usage=Usage(10, 3),
            reasoning=reasoning_text,
            reasoning_details=reasoning_details,
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
        user_message="why is the sky blue?", session_id="s-reason"
    )

    stored = loop.db.get_messages("s-reason")
    # user + assistant
    assistant = [m for m in stored if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].reasoning == reasoning_text
    assert assistant[0].reasoning_details == reasoning_details


@pytest.mark.asyncio
async def test_agent_loop_backwards_compat_no_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider that does NOT populate reasoning — SessionDB stores NULL, no crash."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="hi"),
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
    await loop.run_conversation(user_message="hi", session_id="s-plain")

    stored = loop.db.get_messages("s-plain")
    assistant = [m for m in stored if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].reasoning is None
    assert assistant[0].reasoning_details is None
    assert assistant[0].codex_reasoning_items is None


@pytest.mark.asyncio
async def test_agent_loop_persists_codex_reasoning_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider exposes codex_reasoning_items (OpenAI o1 replay) → persisted."""
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.tools.registry import registry

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    codex_items = [
        {
            "type": "reasoning",
            "summary": [{"type": "text", "text": "plan"}],
            "id": "reasoning_abc",
        },
    ]

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=Usage(5, 2),
            codex_reasoning_items=codex_items,
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )
    await loop.run_conversation(user_message="plan something", session_id="s-codex")

    stored = loop.db.get_messages("s-codex")
    assistant = [m for m in stored if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].codex_reasoning_items == codex_items


# ─── schema migration (pre-existing DB without new columns) ────────────


def test_session_db_schema_migration_adds_reasoning_columns(tmp_path: Path) -> None:
    """Opening a v0-style DB that lacks the new columns triggers ALTER TABLE.

    Simulates a user upgrading OpenComputer — their old sessions.db exists
    but has an older messages table. Migration must be non-destructive
    (no data loss) and idempotent (running twice is fine).
    """
    import sqlite3

    db_path = tmp_path / "old.db"
    # Build an "old" DB manually — messages table without the new columns.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES (1);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            ended_at REAL,
            platform TEXT NOT NULL,
            model TEXT,
            title TEXT,
            message_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_call_id TEXT,
            tool_calls TEXT,
            name TEXT,
            reasoning TEXT,
            timestamp REAL NOT NULL
        );
        INSERT INTO sessions (id, started_at, platform)
        VALUES ('s-legacy', 0.0, 'cli');
        INSERT INTO messages (session_id, role, content, timestamp)
        VALUES ('s-legacy', 'user', 'old message', 0.0);
        """
    )
    conn.close()

    # Open via SessionDB — migration should add the missing columns.
    db = SessionDB(db_path)
    # Appending a message with the new fields must not raise.
    db.append_message(
        "s-legacy",
        Message(
            role="assistant",
            content="migrated reply",
            reasoning="thought",
            reasoning_details=[{"type": "reasoning.text", "text": "x"}],
            codex_reasoning_items=[{"type": "reasoning"}],
        ),
    )
    stored = db.get_messages("s-legacy")
    assistant = [m for m in stored if m.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].reasoning == "thought"
    assert assistant[0].reasoning_details == [{"type": "reasoning.text", "text": "x"}]
    assert assistant[0].codex_reasoning_items == [{"type": "reasoning"}]
