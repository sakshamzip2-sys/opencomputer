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
