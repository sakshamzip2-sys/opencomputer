"""PR #221 follow-up Item 2 — per-session token accumulation.

The ``sessions`` table reserves ``input_tokens`` / ``output_tokens``
columns since schema v1, but no UPDATE site populated them until this
patch. ``SessionDB.add_tokens`` is the new write-side; the agent loop
calls it after each turn with ``ProviderResponse.usage`` deltas.

Coverage:
  * ``add_tokens`` is additive across calls.
  * ``add_tokens`` no-ops on zero / negative inputs.
  * ``run_conversation`` accumulates real numbers across multi-turn runs.
  * ``run_conversation`` tolerates a missing ``Usage`` shape (defensive).
  * Discord ``/usage`` reads the populated columns and renders them.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, Usage

# ---------------------------------------------------------------------------
# SessionDB.add_tokens — the write-side primitive
# ---------------------------------------------------------------------------


def _fresh_db(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("sid-1", platform="cli", model="x")
    return db


def test_add_tokens_bumps_counters(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    db.add_tokens("sid-1", 10, 20)
    row = db.get_session("sid-1")
    assert row is not None
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20


def test_add_tokens_is_additive(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    db.add_tokens("sid-1", 10, 20)
    db.add_tokens("sid-1", 5, 7)
    db.add_tokens("sid-1", 3, 1)
    row = db.get_session("sid-1")
    assert row is not None
    assert row["input_tokens"] == 18
    assert row["output_tokens"] == 28


def test_add_tokens_zero_is_noop(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    db.add_tokens("sid-1", 0, 0)
    row = db.get_session("sid-1")
    assert row is not None
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0


def test_add_tokens_clamps_negatives(tmp_path: Path) -> None:
    """A buggy provider passing negative deltas mustn't drag totals backwards."""
    db = _fresh_db(tmp_path)
    db.add_tokens("sid-1", 10, 10)
    db.add_tokens("sid-1", -5, -3)  # clamped to (0, 0) — no UPDATE
    row = db.get_session("sid-1")
    assert row is not None
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 10


def test_add_tokens_tolerates_none(tmp_path: Path) -> None:
    """Some provider paths surface ``None`` for unknown counts."""
    db = _fresh_db(tmp_path)
    db.add_tokens("sid-1", None, None)  # type: ignore[arg-type]
    row = db.get_session("sid-1")
    assert row is not None
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0


def test_add_tokens_empty_session_id_is_noop(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    # Must not raise.
    db.add_tokens("", 100, 100)
    row = db.get_session("sid-1")
    assert row is not None
    assert row["input_tokens"] == 0


# ---------------------------------------------------------------------------
# AgentLoop.run_conversation — accumulates across turns
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Two-turn provider: first turn returns a tool_use, second turn ends."""

    def __init__(self, usages: list[Usage]) -> None:
        self._usages = list(usages)
        self.complete_calls = 0
        self.name = "fake"

    async def complete(self, *args, **kwargs):  # noqa: ANN001
        idx = self.complete_calls
        self.complete_calls += 1
        usage = self._usages[idx] if idx < len(self._usages) else Usage()
        # Always end-turn (no tool calls) so the loop terminates cleanly.
        return ProviderResponse(
            message=Message(role="assistant", content="hello"),
            stop_reason="end_turn",
            usage=usage,
        )


def _make_loop_with_provider(
    tmp_path: Path,
    provider,  # noqa: ANN001
):
    """Build an AgentLoop wired against ``provider`` + a tmp SessionDB."""
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop

    # Minimal config — point session.db at tmp.
    cfg = Config(
        model=ModelConfig(provider="fake", model="fake-model"),
        loop=LoopConfig(),
        session=SessionConfig(db_path=tmp_path / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
        ),
    )
    db = SessionDB(cfg.session.db_path)
    loop = AgentLoop(provider=provider, config=cfg, db=db)
    return loop, db


@pytest.mark.asyncio
async def test_run_conversation_accumulates_tokens(tmp_path: Path) -> None:
    """Two single-turn runs on the same session sum their usage deltas."""
    provider = _FakeProvider(usages=[Usage(input_tokens=10, output_tokens=20)])
    loop, db = _make_loop_with_provider(tmp_path, provider)

    sid = "fixed-session-token-accum-1"
    await loop.run_conversation(
        user_message="first",
        session_id=sid,
    )

    row = db.get_session(sid)
    assert row is not None, "session row missing after first turn"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20

    # Second turn — same session id reuses the existing row.
    provider._usages = [Usage(input_tokens=5, output_tokens=15)]
    provider.complete_calls = 0
    await loop.run_conversation(
        user_message="second",
        session_id=sid,
    )
    row2 = db.get_session(sid)
    assert row2 is not None
    assert row2["input_tokens"] == 15
    assert row2["output_tokens"] == 35


@pytest.mark.asyncio
async def test_run_conversation_tolerates_zero_usage(tmp_path: Path) -> None:
    """A provider that returns ``Usage()`` (defaults) doesn't crash the loop
    nor populate non-zero counters."""
    provider = _FakeProvider(usages=[Usage()])
    loop, db = _make_loop_with_provider(tmp_path, provider)
    sid = "fixed-session-token-accum-2"
    await loop.run_conversation(
        user_message="hi",
        session_id=sid,
    )
    row = db.get_session(sid)
    assert row is not None
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Discord /usage — now displays real numbers (no more disclaimer)
# ---------------------------------------------------------------------------


def _load_discord_adapter():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "discord_adapter_token_accum_test",
        Path(__file__).resolve().parent.parent
        / "extensions" / "discord" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_discord_adapter():
    mod = _load_discord_adapter()
    fake_client = MagicMock()
    fake_state = MagicMock()
    fake_state._command_tree = None
    fake_client._connection = fake_state
    with (
        patch.dict(os.environ, dict(os.environ), clear=True),
        patch("discord.Client", return_value=fake_client),
    ):
        return mod.DiscordAdapter(config={"bot_token": "fake-token"})


@pytest.mark.asyncio
async def test_discord_usage_renders_real_numbers() -> None:
    """``/usage`` shows ``input=N output=M`` with the real values from the row."""
    a = _make_discord_adapter()
    interaction = MagicMock()
    interaction.channel_id = 7
    interaction.response.send_message = AsyncMock()

    sid = a._dispatch_thread_session("7", None)
    db_mock = MagicMock()
    db_mock.get_session = MagicMock(return_value={
        "id": sid,
        "input_tokens": 100,
        "output_tokens": 250,
        "message_count": 4,
    })
    db_mock.query_tool_usage = MagicMock(return_value=[])
    a._session_db = db_mock

    await a._handle_usage_slash(interaction)
    msg = interaction.response.send_message.await_args.args[0]
    assert "input=100" in msg
    assert "output=250" in msg
    assert "messages=4" in msg
    # No more "not yet wired" disclaimer — this is now real data.
    assert "not yet wired" not in msg.lower()


@pytest.mark.asyncio
async def test_discord_usage_zero_is_honest_not_disclaimer() -> None:
    """When tokens are 0 (provider didn't surface usage), show ``input=0
    output=0`` rather than a "not yet wired" caveat — the column IS now
    wired; zero is a real signal."""
    a = _make_discord_adapter()
    interaction = MagicMock()
    interaction.channel_id = 8
    interaction.response.send_message = AsyncMock()

    sid = a._dispatch_thread_session("8", None)
    db_mock = MagicMock()
    db_mock.get_session = MagicMock(return_value={
        "id": sid,
        "input_tokens": 0,
        "output_tokens": 0,
        "message_count": 1,
    })
    db_mock.query_tool_usage = MagicMock(return_value=[])
    a._session_db = db_mock

    await a._handle_usage_slash(interaction)
    msg = interaction.response.send_message.await_args.args[0]
    assert "input=0" in msg
    assert "output=0" in msg
    assert "not yet wired" not in msg.lower()
