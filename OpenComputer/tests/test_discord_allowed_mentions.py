"""Discord allowed_mentions safe defaults (PR 4.3).

Discord allows the bot to ping @everyone / @here / arbitrary roles
unless the call explicitly disables them. A runaway agent could
accidentally page a whole guild — so we ship a safe default
(``AllowedMentions(everyone=False, roles=False, users=True,
replied_user=True)``) and pass it on every ``channel.send`` and
``message.edit``.

Operators can opt back in via env:

- ``DISCORD_ALLOW_MENTION_EVERYONE`` → enable @everyone / @here
- ``DISCORD_ALLOW_MENTION_ROLES`` → enable @role pings
- ``DISCORD_ALLOW_MENTION_USERS`` (default ON)
- ``DISCORD_ALLOW_MENTION_REPLIED_USER`` (default ON)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from extensions.discord.adapter import DiscordAdapter


def _make_adapter() -> DiscordAdapter:
    a = DiscordAdapter({"bot_token": "test"})
    a._bot_user_id = 999
    return a


# ---------------------------------------------------------------------------
# _build_allowed_mentions: default + env-override semantics
# ---------------------------------------------------------------------------


class TestBuildAllowedMentions:
    def test_default_blocks_everyone_and_roles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DISCORD_ALLOW_MENTION_EVERYONE", raising=False)
        monkeypatch.delenv("DISCORD_ALLOW_MENTION_ROLES", raising=False)
        monkeypatch.delenv("DISCORD_ALLOW_MENTION_USERS", raising=False)
        monkeypatch.delenv(
            "DISCORD_ALLOW_MENTION_REPLIED_USER", raising=False
        )
        a = _make_adapter()
        am = a._build_allowed_mentions()
        assert isinstance(am, discord.AllowedMentions)
        assert am.everyone is False
        assert am.roles is False
        # Users and replied_user default ON (sensible defaults: a reply
        # without a ping looks broken; user pings are how DMs/threads
        # surface notifications).
        assert am.users is True
        assert am.replied_user is True

    def test_env_flips_everyone_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_ALLOW_MENTION_EVERYONE", "1")
        a = _make_adapter()
        am = a._build_allowed_mentions()
        assert am.everyone is True

    def test_env_flips_roles_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_ALLOW_MENTION_ROLES", "true")
        a = _make_adapter()
        am = a._build_allowed_mentions()
        assert am.roles is True

    def test_env_flips_users_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_ALLOW_MENTION_USERS", "0")
        a = _make_adapter()
        am = a._build_allowed_mentions()
        assert am.users is False

    def test_env_flips_replied_user_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_ALLOW_MENTION_REPLIED_USER", "no")
        a = _make_adapter()
        am = a._build_allowed_mentions()
        assert am.replied_user is False

    def test_truthy_strings_recognised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for truthy in ("1", "true", "TRUE", "yes", "YES", "on"):
            monkeypatch.setenv("DISCORD_ALLOW_MENTION_EVERYONE", truthy)
            a = _make_adapter()
            assert a._build_allowed_mentions().everyone is True

    def test_unrecognised_treated_as_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Any unrecognised value falls back to default — the default
        # for everyone is False, so this stays False.
        monkeypatch.setenv("DISCORD_ALLOW_MENTION_EVERYONE", "maybe")
        a = _make_adapter()
        assert a._build_allowed_mentions().everyone is False


# ---------------------------------------------------------------------------
# send() passes allowed_mentions to channel.send
# ---------------------------------------------------------------------------


class TestSendWiresAllowedMentions:
    @pytest.mark.asyncio
    async def test_send_passes_allowed_mentions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DISCORD_ALLOW_MENTION_EVERYONE", raising=False)
        monkeypatch.delenv("DISCORD_ALLOW_MENTION_ROLES", raising=False)

        a = _make_adapter()
        channel = MagicMock()
        sent_msg = MagicMock()
        sent_msg.id = 12345
        channel.send = AsyncMock(return_value=sent_msg)
        a._channel_cache["c-1"] = channel

        result = await a.send("c-1", "hello world")
        assert result.success is True

        channel.send.assert_called_once()
        kwargs = channel.send.call_args.kwargs
        assert "allowed_mentions" in kwargs
        am = kwargs["allowed_mentions"]
        assert isinstance(am, discord.AllowedMentions)
        assert am.everyone is False
        assert am.roles is False

    @pytest.mark.asyncio
    async def test_edit_passes_allowed_mentions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DISCORD_ALLOW_MENTION_EVERYONE", raising=False)

        a = _make_adapter()
        channel = MagicMock()
        msg = MagicMock()
        msg.id = 99
        msg.edit = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=msg)
        a._channel_cache["c-1"] = channel

        result = await a.edit_message("c-1", "99", "updated text")
        assert result.success is True

        msg.edit.assert_awaited_once()
        kwargs = msg.edit.call_args.kwargs
        assert "allowed_mentions" in kwargs
        assert kwargs["allowed_mentions"].everyone is False
