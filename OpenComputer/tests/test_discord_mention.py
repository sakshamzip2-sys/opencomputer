"""Tests for Discord mention-gating + multi-bot disambiguation (PR 3b.1).

Covers ``DiscordAdapter._should_process`` — the gate the on_message
handler runs before dispatching. We construct a fake ``discord.Message``
with the attributes the adapter reads (``mentions``, ``role_mentions``,
``author``, ``guild``, ``channel``, ``content``, ``id``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "discord_adapter_pr3b1_mention",
        Path(__file__).resolve().parent.parent / "extensions" / "discord" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DiscordAdapter


def _make_adapter(
    *,
    bot_user_id: int = 12345,
    require_mention: bool = False,
    allowed_users: list[Any] | None = None,
    allowed_roles: list[Any] | None = None,
    allow_bots: str = "none",
):
    """Construct a DiscordAdapter without instantiating discord.Client."""
    DiscordAdapter = _load_adapter()
    a = object.__new__(DiscordAdapter)
    a.config = {}
    a.token = "fake"
    bot_user = SimpleNamespace(id=bot_user_id, bot=True)
    fake_client = MagicMock()
    fake_client.user = bot_user
    # mentioned_in: the canonical discord.py check. Implement as
    # "is this user in msg.mentions or do role-mentions cover it".
    def _mentioned_in(msg: Any) -> bool:
        if any(getattr(u, "id", None) == bot_user.id for u in (msg.mentions or [])):
            return True
        # role_mentions: any role the bot carries that's mentioned.
        if hasattr(msg, "role_mentions"):
            return False
        return False
    bot_user.mentioned_in = _mentioned_in  # type: ignore[attr-defined]
    a._client = fake_client
    a._bot_user_id = bot_user_id
    a._channel_cache = {}
    a._client_task = None
    a._ready_event = MagicMock()
    a._require_mention = require_mention
    a._allowed_users = {str(u) for u in (allowed_users or [])}
    a._allowed_roles = {str(r) for r in (allowed_roles or [])}
    a._allow_bots = allow_bots
    return a


def _make_msg(
    *,
    author_id: int = 999,
    author_is_bot: bool = False,
    author_role_ids: list[int] | None = None,
    content: str = "hi",
    is_dm: bool = False,
    mentions: list[Any] | None = None,
):
    author = SimpleNamespace(
        id=author_id,
        bot=author_is_bot,
        roles=[SimpleNamespace(id=r) for r in (author_role_ids or [])],
    )
    msg = SimpleNamespace(
        author=author,
        content=content,
        guild=None if is_dm else SimpleNamespace(id=42),
        channel=SimpleNamespace(id=100),
        id=7777,
        mentions=mentions or [],
    )
    return msg


# ---------------------------------------------------------------------------
# require_mention
# ---------------------------------------------------------------------------


class TestRequireMention:
    def test_default_false_processes_unmentioned_messages(self) -> None:
        a = _make_adapter()
        msg = _make_msg(content="hello")
        assert a._should_process(msg) is True

    def test_when_true_unmentioned_guild_message_skipped(self) -> None:
        a = _make_adapter(require_mention=True)
        msg = _make_msg(content="hello")
        assert a._should_process(msg) is False

    def test_when_true_dm_still_processed(self) -> None:
        """DMs are implicit @-mentions — they bypass require_mention."""
        a = _make_adapter(require_mention=True)
        msg = _make_msg(content="hello", is_dm=True)
        assert a._should_process(msg) is True

    def test_when_true_mentioned_message_processed(self) -> None:
        a = _make_adapter(require_mention=True, bot_user_id=12345)
        bot_obj = SimpleNamespace(id=12345, bot=True)
        msg = _make_msg(content="<@12345> hello", mentions=[bot_obj])
        assert a._should_process(msg) is True


# ---------------------------------------------------------------------------
# Multi-bot disambiguation
# ---------------------------------------------------------------------------


class TestMultiBotDisambiguation:
    def test_other_bot_mentioned_we_arent_silent(self) -> None:
        """Even with require_mention=False, defer when a different bot is targeted."""
        a = _make_adapter(require_mention=False, bot_user_id=12345)
        other_bot = SimpleNamespace(id=99999, bot=True)
        msg = _make_msg(content="<@99999> hello", mentions=[other_bot])
        assert a._should_process(msg) is False

    def test_we_are_mentioned_too_processed(self) -> None:
        a = _make_adapter(require_mention=False, bot_user_id=12345)
        us = SimpleNamespace(id=12345, bot=True)
        other = SimpleNamespace(id=99999, bot=True)
        msg = _make_msg(content="<@12345> <@99999> hi", mentions=[us, other])
        assert a._should_process(msg) is True

    def test_human_mentioned_alongside_other_bot_still_silent(self) -> None:
        """A human + another bot mentioned, we're NOT — still silent."""
        a = _make_adapter(require_mention=False, bot_user_id=12345)
        other_bot = SimpleNamespace(id=99999, bot=True)
        human = SimpleNamespace(id=42, bot=False)
        msg = _make_msg(content="hi", mentions=[other_bot, human])
        assert a._should_process(msg) is False

    def test_only_human_mentioned_processed(self) -> None:
        a = _make_adapter(require_mention=False, bot_user_id=12345)
        human = SimpleNamespace(id=42, bot=False)
        msg = _make_msg(content="hi", mentions=[human])
        assert a._should_process(msg) is True


# ---------------------------------------------------------------------------
# Bot author policy (allow_bots)
# ---------------------------------------------------------------------------


class TestAllowBots:
    def test_default_none_drops_bot_author(self) -> None:
        a = _make_adapter()  # allow_bots="none"
        msg = _make_msg(author_is_bot=True)
        assert a._should_process(msg) is False

    def test_mentions_allows_bot_when_we_mentioned(self) -> None:
        a = _make_adapter(allow_bots="mentions", bot_user_id=12345)
        us = SimpleNamespace(id=12345, bot=True)
        msg = _make_msg(author_is_bot=True, mentions=[us])
        assert a._should_process(msg) is True

    def test_mentions_drops_bot_when_unmentioned(self) -> None:
        a = _make_adapter(allow_bots="mentions", bot_user_id=12345)
        msg = _make_msg(author_is_bot=True)
        assert a._should_process(msg) is False

    def test_all_processes_any_bot(self) -> None:
        a = _make_adapter(allow_bots="all")
        msg = _make_msg(author_is_bot=True)
        assert a._should_process(msg) is True

    def test_invalid_value_falls_back_to_none(self) -> None:
        # Re-run the __init__ validation path: invalid string normalises
        # to "none" so a bot-author message is dropped.
        DiscordAdapter = _load_adapter()
        from unittest.mock import patch

        with patch("discord.Client"):
            a = DiscordAdapter(
                config={"bot_token": "x", "allow_bots": "garbage"}
            )
        assert a._allow_bots == "none"


# ---------------------------------------------------------------------------
# Mention detection — role mentions surface via msg.mentions
# ---------------------------------------------------------------------------


class TestMentionDetectionRoles:
    def test_role_mention_resolves_to_bot_in_mentions_list(self) -> None:
        """When a @role-mention resolves to a list including the bot,
        Discord populates ``msg.mentions`` with each user (incl. the bot).
        Our scan over msg.mentions must catch it."""
        a = _make_adapter(require_mention=True, bot_user_id=12345)
        us = SimpleNamespace(id=12345, bot=True)
        msg = _make_msg(content="@team please respond", mentions=[us])
        assert a._should_process(msg) is True
