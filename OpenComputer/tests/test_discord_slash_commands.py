"""Tests for Discord slash command tree (PR 6.1).

Covers:
  * the full command tree is registered (12 commands)
  * sync policies — safe / bulk / off
  * /steer routes to SteerRegistry
  * /side derives a separate session id
  * /thread invokes _create_thread under parent
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_adapter_mod():
    spec = importlib.util.spec_from_file_location(
        "discord_adapter_pr6_slash",
        Path(__file__).resolve().parent.parent
        / "extensions" / "discord" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_real_adapter(env: dict[str, str] | None = None):
    """Build a DiscordAdapter via real ``__init__`` so the slash tree
    actually gets registered. Patches discord.Client so no network."""
    mod = _load_adapter_mod()
    DiscordAdapter = mod.DiscordAdapter

    real_env = dict(os.environ)
    if env:
        real_env.update(env)

    # Build a fresh fake client whose _connection._command_tree is None
    # so app_commands.CommandTree(client) succeeds.
    fake_client = MagicMock()
    fake_state = MagicMock()
    fake_state._command_tree = None
    fake_client._connection = fake_state

    with (
        patch.dict(os.environ, real_env, clear=True),
        patch("discord.Client", return_value=fake_client),
    ):
        a = DiscordAdapter(config={"bot_token": "fake-token"})
    return a, fake_client


# ---------------------------------------------------------------------------
# Tree registration
# ---------------------------------------------------------------------------


EXPECTED_COMMANDS = {
    "ask",
    "reset",
    "status",
    "stop",
    "steer",
    "queue",
    "background",
    "side",
    "title",
    "resume",
    "usage",
    "thread",
}


class TestSlashTreeRegistration:
    def test_all_12_commands_registered(self) -> None:
        a, _ = _make_real_adapter()
        assert a._tree is not None, "command tree should be created"
        names = {c.name for c in a._tree.get_commands()}
        assert names == EXPECTED_COMMANDS, (
            f"expected {EXPECTED_COMMANDS}, got {names}"
        )

    def test_tree_attached_to_client(self) -> None:
        a, _ = _make_real_adapter()
        assert a._tree.client is a._client


# ---------------------------------------------------------------------------
# Sync policy
# ---------------------------------------------------------------------------


class TestSyncPolicy:
    def test_default_mode_is_safe(self) -> None:
        a, _ = _make_real_adapter(env={})
        # If env not set, defaults to "safe"
        assert a._command_sync_mode == "safe"

    def test_bulk_mode_from_env(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "bulk"})
        assert a._command_sync_mode == "bulk"

    def test_off_mode_from_env(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "off"})
        assert a._command_sync_mode == "off"

    def test_invalid_mode_falls_back_to_safe(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "garbage"})
        assert a._command_sync_mode == "safe"

    @pytest.mark.asyncio
    async def test_off_mode_skips_sync(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "off"})
        a._tree.sync = AsyncMock()
        a._tree.fetch_commands = AsyncMock()
        await a._sync_slash_commands()
        a._tree.sync.assert_not_awaited()
        a._tree.fetch_commands.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bulk_mode_calls_sync(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "bulk"})
        a._tree.sync = AsyncMock()
        a._tree.fetch_commands = AsyncMock()
        await a._sync_slash_commands()
        a._tree.sync.assert_awaited_once()
        # Bulk doesn't diff
        a._tree.fetch_commands.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safe_mode_skips_sync_when_remote_matches(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "safe"})
        # Remote returns exactly the same command names
        remote_cmds = [SimpleNamespace(name=n) for n in EXPECTED_COMMANDS]
        a._tree.sync = AsyncMock()
        a._tree.fetch_commands = AsyncMock(return_value=remote_cmds)
        await a._sync_slash_commands()
        a._tree.fetch_commands.assert_awaited_once()
        a._tree.sync.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safe_mode_syncs_when_diff(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "safe"})
        # Remote missing /thread → diff
        remote_cmds = [
            SimpleNamespace(name=n) for n in EXPECTED_COMMANDS - {"thread"}
        ]
        a._tree.sync = AsyncMock()
        a._tree.fetch_commands = AsyncMock(return_value=remote_cmds)
        await a._sync_slash_commands()
        a._tree.sync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_safe_mode_falls_back_on_fetch_error(self) -> None:
        a, _ = _make_real_adapter(env={"DISCORD_COMMAND_SYNC": "safe"})
        a._tree.sync = AsyncMock()
        a._tree.fetch_commands = AsyncMock(side_effect=RuntimeError("boom"))
        await a._sync_slash_commands()
        # Falls through to a full sync
        a._tree.sync.assert_awaited_once()


# ---------------------------------------------------------------------------
# /steer routes to SteerRegistry
# ---------------------------------------------------------------------------


class TestSteerSlashRoutes:
    @pytest.mark.asyncio
    async def test_steer_submits_to_registry(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 12345
        interaction.user.id = 999
        interaction.id = 7
        interaction.response.send_message = AsyncMock()

        registry_mock = MagicMock()
        registry_mock.has_pending = MagicMock(return_value=False)
        registry_mock.submit = MagicMock()

        with patch(
            "opencomputer.agent.steer.default_registry", registry_mock
        ):
            await a._handle_steer_slash(interaction, "do the thing")

        registry_mock.submit.assert_called_once()
        args, _ = registry_mock.submit.call_args
        # First arg is the session id (32-char hex), second is body
        assert len(args[0]) == 32
        assert args[1] == "do the thing"
        interaction.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_steer_empty_shows_usage(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 12345
        interaction.response.send_message = AsyncMock()

        await a._handle_steer_slash(interaction, "   ")

        # Empty body → usage message, no registry call
        interaction.response.send_message.assert_awaited_once()
        assert "usage" in interaction.response.send_message.await_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_steer_override_logs_pending(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 12345
        interaction.response.send_message = AsyncMock()

        registry_mock = MagicMock()
        registry_mock.has_pending = MagicMock(return_value=True)
        registry_mock.submit = MagicMock()

        with patch(
            "opencomputer.agent.steer.default_registry", registry_mock
        ):
            await a._handle_steer_slash(interaction, "new nudge")

        msg = interaction.response.send_message.await_args.args[0]
        assert "override" in msg.lower()


# ---------------------------------------------------------------------------
# /side
# ---------------------------------------------------------------------------


class TestSideSlash:
    @pytest.mark.asyncio
    async def test_side_uses_distinct_session(self) -> None:
        from opencomputer.gateway.dispatch import session_id_for

        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 7777
        interaction.user.id = 1
        interaction.id = 4242
        interaction.response.send_message = AsyncMock()

        # Stub handle_message so we don't reach the gateway
        captured = []

        async def _fake_handle(event):
            captured.append(event)

        a.handle_message = _fake_handle  # type: ignore[assignment]
        await a._handle_side_slash(interaction, "side prompt")

        assert captured, "/side should dispatch a MessageEvent"
        ev = captured[0]
        side_sid = ev.metadata["side_session_id"]
        # Must NOT equal the main session id for this chat
        main_sid = session_id_for("discord", "7777")
        assert side_sid != main_sid


# ---------------------------------------------------------------------------
# /thread
# ---------------------------------------------------------------------------


class TestThreadSlash:
    @pytest.mark.asyncio
    async def test_thread_create_invokes_helper(self) -> None:
        import discord

        a, _ = _make_real_adapter()
        interaction = MagicMock()
        # Forum parent channel
        forum = MagicMock()
        forum.type = discord.ChannelType.forum
        forum.id = 500
        forum.create_thread = AsyncMock(
            return_value=SimpleNamespace(thread=SimpleNamespace(id=601))
        )
        interaction.channel = forum
        interaction.user.display_name = "alice"
        interaction.response.send_message = AsyncMock()

        # Pre-cache so _resolve_channel finds it
        a._channel_cache["500"] = forum

        await a._handle_thread_create_slash(interaction, "my thread")
        forum.create_thread.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "601" in msg

    @pytest.mark.asyncio
    async def test_thread_create_no_channel_responds(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel = None
        interaction.response.send_message = AsyncMock()

        await a._handle_thread_create_slash(interaction, None)
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "channel" in msg.lower()


# ---------------------------------------------------------------------------
# /reset, /status, /stop — basic smoke
# ---------------------------------------------------------------------------


class TestSimpleSlashHandlers:
    @pytest.mark.asyncio
    async def test_reset_responds_ephemerally(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 1
        interaction.response.send_message = AsyncMock()
        await a._handle_reset_slash(interaction)
        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_status_includes_session_prefix(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 42
        interaction.response.send_message = AsyncMock()
        await a._handle_status_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "session=" in msg
        assert "channel=42" in msg

    @pytest.mark.asyncio
    async def test_stop_calls_steer_with_sentinel(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 99
        interaction.response.send_message = AsyncMock()

        registry_mock = MagicMock()
        registry_mock.submit = MagicMock()
        with patch(
            "opencomputer.agent.steer.default_registry", registry_mock
        ):
            await a._handle_stop_slash(interaction)
        registry_mock.submit.assert_called_once()
        body = registry_mock.submit.call_args.args[1]
        assert body == "__STOP__"


# ---------------------------------------------------------------------------
# /reset, /queue, /resume, /usage — backend-wired (PR #221 follow-up)
# ---------------------------------------------------------------------------


class TestResetSlashWired:
    @pytest.mark.asyncio
    async def test_reset_calls_end_session(self) -> None:
        """/reset must invoke SessionDB.end_session for the chat's sid."""
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 1234
        interaction.response.send_message = AsyncMock()

        db_mock = MagicMock()
        db_mock.end_session = MagicMock()
        a._session_db = db_mock  # bypass plugin_registry resolution

        await a._handle_reset_slash(interaction)
        db_mock.end_session.assert_called_once()
        # Reply mentions reset + ephemeral
        msg = interaction.response.send_message.await_args.args[0]
        assert "reset" in msg.lower()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_reset_handles_missing_db(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 1
        interaction.response.send_message = AsyncMock()

        # Force resolver to return None
        a._session_db = None
        with patch.object(a, "_resolve_session_db", return_value=None):
            await a._handle_reset_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "unavailable" in msg.lower() or "no sessiondb" in msg.lower()


class TestQueueSlashWired:
    @pytest.mark.asyncio
    async def test_queue_lists_only_this_chat(self) -> None:
        """/queue filters to platform=discord + chat_id."""
        from opencomputer.gateway.outgoing_queue import OutgoingMessage

        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 555
        interaction.response.send_message = AsyncMock()

        # Build queue rows: one matching, one for telegram, one for
        # different discord chat.
        rows = [
            OutgoingMessage(
                id="aaa", platform="discord", chat_id="555",
                body="for me", status="queued", enqueued_at=1.0,
            ),
            OutgoingMessage(
                id="bbb", platform="telegram", chat_id="555",
                body="other platform", status="queued", enqueued_at=2.0,
            ),
            OutgoingMessage(
                id="ccc", platform="discord", chat_id="999",
                body="other chat", status="queued", enqueued_at=3.0,
            ),
        ]
        queue_mock = MagicMock()
        queue_mock.list_ = MagicMock(return_value=rows)
        a._outgoing_queue = queue_mock

        await a._handle_queue_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        # Only the matching one shows up
        assert "aaa" in msg
        assert "bbb" not in msg
        assert "ccc" not in msg
        assert "1 queued" in msg

    @pytest.mark.asyncio
    async def test_queue_empty(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 1
        interaction.response.send_message = AsyncMock()

        queue_mock = MagicMock()
        queue_mock.list_ = MagicMock(return_value=[])
        a._outgoing_queue = queue_mock

        await a._handle_queue_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "empty" in msg.lower()

    @pytest.mark.asyncio
    async def test_queue_unbound(self) -> None:
        """No OutgoingQueue available → honest 'unavailable' reply."""
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 1
        interaction.response.send_message = AsyncMock()

        a._outgoing_queue = None
        with patch.object(a, "_resolve_outgoing_queue", return_value=None):
            await a._handle_queue_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "unavailable" in msg.lower()


class TestResumeSlashWired:
    @pytest.mark.asyncio
    async def test_resume_no_session_says_so(self) -> None:
        """No session row → user gets a 'no recent session' reply."""
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 1
        interaction.response.send_message = AsyncMock()

        db_mock = MagicMock()
        db_mock.get_session = MagicMock(return_value=None)
        a._session_db = db_mock

        await a._handle_resume_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "no recent session" in msg.lower()

    @pytest.mark.asyncio
    async def test_resume_existing_ended_session_reopens(self) -> None:
        """Ended session → ended_at gets cleared via UPDATE."""
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 1
        interaction.response.send_message = AsyncMock()

        # Use a real on-disk SessionDB so the _txn UPDATE path executes.
        import tempfile
        from pathlib import Path

        from opencomputer.agent.state import SessionDB

        with tempfile.TemporaryDirectory() as td:
            db = SessionDB(Path(td) / "s.db")
            sid = a._dispatch_thread_session("1", None)
            db.create_session(sid)
            db.end_session(sid)
            assert db.get_session(sid)["ended_at"] is not None

            a._session_db = db
            await a._handle_resume_slash(interaction)
            # ended_at cleared after resume
            assert db.get_session(sid)["ended_at"] is None
        msg = interaction.response.send_message.await_args.args[0]
        assert "resumed" in msg.lower()


class TestUsageSlashWired:
    @pytest.mark.asyncio
    async def test_usage_reports_session_stats(self) -> None:
        """/usage renders message_count + tool-call aggregate."""
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 42
        interaction.response.send_message = AsyncMock()

        sid = a._dispatch_thread_session("42", None)
        db_mock = MagicMock()
        db_mock.get_session = MagicMock(return_value={
            "id": sid,
            "started_at": 0.0,
            "ended_at": None,
            "platform": "discord",
            "model": "x",
            "title": "",
            "message_count": 7,
            "input_tokens": 0,
            "output_tokens": 0,
        })
        db_mock.query_tool_usage = MagicMock(return_value=[
            {"key": sid, "calls": 3, "errors": 0,
             "avg_duration_ms": 10.0, "total_duration_ms": 30.0,
             "error_rate": 0.0},
        ])
        a._session_db = db_mock

        await a._handle_usage_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "messages=7" in msg
        assert "tool calls=3" in msg
        # Honest about token tracking when columns are zero.
        assert "not yet wired" in msg.lower() or "input=" in msg.lower()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_usage_with_populated_tokens(self) -> None:
        """When tokens > 0, the line shows them numerically."""
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 42
        interaction.response.send_message = AsyncMock()

        sid = a._dispatch_thread_session("42", None)
        db_mock = MagicMock()
        db_mock.get_session = MagicMock(return_value={
            "id": sid,
            "input_tokens": 1234,
            "output_tokens": 567,
            "message_count": 2,
        })
        db_mock.query_tool_usage = MagicMock(return_value=[])
        a._session_db = db_mock

        await a._handle_usage_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "input=1234" in msg
        assert "output=567" in msg

    @pytest.mark.asyncio
    async def test_usage_no_session(self) -> None:
        a, _ = _make_real_adapter()
        interaction = MagicMock()
        interaction.channel_id = 42
        interaction.response.send_message = AsyncMock()

        db_mock = MagicMock()
        db_mock.get_session = MagicMock(return_value=None)
        a._session_db = db_mock

        await a._handle_usage_slash(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "no session" in msg.lower()
