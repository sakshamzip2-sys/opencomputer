"""Tests for Discord forum + thread helpers (PR 6.1).

Covers:
  * forum-parent detection
  * thread → parent resolution
  * effective-topic walk
  * auto-archive validation
  * friendly thread name formatting
  * auto-thread heuristic
  * thread session id derivation
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest


def _load_threads():
    spec = importlib.util.spec_from_file_location(
        "discord_threads_pr6",
        Path(__file__).resolve().parent.parent
        / "extensions" / "discord" / "threads.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_adapter_mod():
    spec = importlib.util.spec_from_file_location(
        "discord_adapter_pr6",
        Path(__file__).resolve().parent.parent
        / "extensions" / "discord" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Channel-type detection
# ---------------------------------------------------------------------------


class TestForumParentDetection:
    def test_forum_channel_is_forum_parent(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.forum, id=1)
        assert threads._is_forum_parent(ch) is True

    def test_text_channel_not_forum_parent(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.text, id=1)
        assert threads._is_forum_parent(ch) is False

    def test_thread_not_forum_parent(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.public_thread, id=1)
        assert threads._is_forum_parent(ch) is False

    def test_none_channel_safe(self) -> None:
        threads = _load_threads()
        assert threads._is_forum_parent(None) is False
        assert threads._is_forum_parent(SimpleNamespace()) is False


class TestThreadDetection:
    def test_public_thread_detected(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.public_thread, id=1)
        assert threads._is_thread(ch) is True

    def test_private_thread_detected(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.private_thread, id=1)
        assert threads._is_thread(ch) is True

    def test_text_channel_not_thread(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.text, id=1)
        assert threads._is_thread(ch) is False

    def test_forum_parent_not_thread(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.forum, id=1)
        assert threads._is_thread(ch) is False


# ---------------------------------------------------------------------------
# Parent-channel resolution
# ---------------------------------------------------------------------------


class TestParentResolution:
    def test_thread_resolves_to_parent(self) -> None:
        threads = _load_threads()
        parent = SimpleNamespace(type=discord.ChannelType.forum, id=1000)
        thread = SimpleNamespace(
            type=discord.ChannelType.public_thread,
            id=1001,
            parent=parent,
        )
        assert threads._get_parent_channel_id(thread) == 1000

    def test_text_channel_returns_self(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.text, id=42)
        assert threads._get_parent_channel_id(ch) == 42

    def test_thread_with_only_parent_id(self) -> None:
        threads = _load_threads()
        thread = SimpleNamespace(
            type=discord.ChannelType.public_thread,
            id=10,
            parent_id=99,
            parent=None,
        )
        # parent is None, parent_id used as fallback
        assert threads._get_parent_channel_id(thread) == 99

    def test_none_returns_none(self) -> None:
        threads = _load_threads()
        assert threads._get_parent_channel_id(None) is None


class TestEffectiveTopic:
    def test_text_channel_uses_own_topic(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(
            type=discord.ChannelType.text, id=1, topic="general chat"
        )
        assert threads._get_effective_topic(ch) == "general chat"

    def test_thread_inherits_parent_topic(self) -> None:
        threads = _load_threads()
        parent = SimpleNamespace(
            type=discord.ChannelType.text, id=1, topic="parent topic"
        )
        thread = SimpleNamespace(
            type=discord.ChannelType.public_thread,
            id=2,
            parent=parent,
            topic=None,
        )
        assert threads._get_effective_topic(thread) == "parent topic"


# ---------------------------------------------------------------------------
# Auto-archive validation
# ---------------------------------------------------------------------------


class TestAutoArchiveValidation:
    @pytest.mark.parametrize("d", [60, 1440, 4320, 10080])
    def test_valid_durations_pass(self, d: int) -> None:
        threads = _load_threads()
        assert threads._validate_auto_archive_duration(d) == d

    @pytest.mark.parametrize("d", [0, 30, 100, 9999, -1])
    def test_invalid_durations_raise(self, d: int) -> None:
        threads = _load_threads()
        with pytest.raises(ValueError, match="auto_archive_duration"):
            threads._validate_auto_archive_duration(d)


# ---------------------------------------------------------------------------
# Friendly thread name
# ---------------------------------------------------------------------------


class TestThreadNameFormatting:
    def test_first_line_used(self) -> None:
        threads = _load_threads()
        out = threads._format_thread_chat_name(
            text="What's the weather?\nMore detail here"
        )
        assert out == "What's the weather?"

    def test_strips_slash_command(self) -> None:
        threads = _load_threads()
        out = threads._format_thread_chat_name(text="/ask How does X work?")
        assert out == "How does X work?"

    def test_falls_back_to_author(self) -> None:
        threads = _load_threads()
        out = threads._format_thread_chat_name(text="", author_name="alice")
        assert out == "chat with alice"

    def test_default_fallback(self) -> None:
        threads = _load_threads()
        out = threads._format_thread_chat_name()
        assert out == "Conversation"

    def test_truncates_at_100(self) -> None:
        threads = _load_threads()
        long = "x" * 250
        out = threads._format_thread_chat_name(text=long)
        assert len(out) <= 100
        assert out.endswith("…")


# ---------------------------------------------------------------------------
# Auto-thread heuristic
# ---------------------------------------------------------------------------


class TestAutoThreadHeuristic:
    def test_long_message_in_forum_triggers(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.forum, id=1)
        msg = SimpleNamespace(channel=ch, content="x" * 400)
        assert threads._should_auto_thread(msg) is True

    def test_short_message_in_forum_skips(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.forum, id=1)
        msg = SimpleNamespace(channel=ch, content="lol")
        assert threads._should_auto_thread(msg) is False

    def test_long_message_in_text_channel_skips(self) -> None:
        threads = _load_threads()
        ch = SimpleNamespace(type=discord.ChannelType.text, id=1)
        msg = SimpleNamespace(channel=ch, content="x" * 400)
        assert threads._should_auto_thread(msg) is False


# ---------------------------------------------------------------------------
# Thread creation (mocked discord.py)
# ---------------------------------------------------------------------------


class TestThreadCreation:
    @pytest.mark.asyncio
    async def test_create_thread_in_forum_uses_starter(self) -> None:
        threads = _load_threads()
        new_thread = MagicMock()
        new_thread.id = 12345
        # Forum.create_thread returns ThreadWithMessage in newer discord.py
        wrapper = MagicMock()
        wrapper.thread = new_thread
        forum_parent = MagicMock()
        forum_parent.type = discord.ChannelType.forum
        forum_parent.create_thread = AsyncMock(return_value=wrapper)
        msg = SimpleNamespace(content="hello world")

        new_id = await threads._create_thread_via_channel(
            forum_parent,
            name="my chat",
            auto_archive_duration=1440,
            message=msg,
        )
        assert new_id == 12345
        forum_parent.create_thread.assert_awaited_once()
        kwargs = forum_parent.create_thread.await_args.kwargs
        assert kwargs["name"] == "my chat"
        assert kwargs["auto_archive_duration"] == 1440
        assert kwargs["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_create_thread_text_channel_with_message(self) -> None:
        threads = _load_threads()
        msg = MagicMock()
        msg.create_thread = AsyncMock(return_value=SimpleNamespace(id=999))
        text_ch = MagicMock()
        text_ch.type = discord.ChannelType.text

        new_id = await threads._create_thread_via_channel(
            text_ch,
            name="thread name",
            auto_archive_duration=60,
            message=msg,
        )
        assert new_id == 999
        msg.create_thread.assert_awaited_once_with(
            name="thread name", auto_archive_duration=60
        )

    @pytest.mark.asyncio
    async def test_invalid_duration_rejected_before_api(self) -> None:
        threads = _load_threads()
        forum = MagicMock()
        forum.type = discord.ChannelType.forum
        forum.create_thread = AsyncMock()
        with pytest.raises(ValueError, match="auto_archive_duration"):
            await threads._create_thread_via_channel(
                forum, name="x", auto_archive_duration=999
            )
        forum.create_thread.assert_not_awaited()


# ---------------------------------------------------------------------------
# Adapter-level thread session derivation
# ---------------------------------------------------------------------------


def _make_bare_adapter():
    """Construct a DiscordAdapter without instantiating discord.Client."""
    DiscordAdapter = _load_adapter_mod().DiscordAdapter
    a = object.__new__(DiscordAdapter)
    a.config = {}
    a.token = "fake"
    a._client = MagicMock()
    a._bot_user_id = 12345
    a._channel_cache = {}
    a._client_task = None
    a._ready_event = MagicMock()
    a._require_mention = False
    a._allowed_users = set()
    a._allowed_roles = set()
    a._allow_bots = "none"
    a._command_sync_mode = "safe"
    a._tree = None
    a._thread_sessions = {}
    return a


class TestThreadSessionDerivation:
    def test_thread_session_differs_from_parent(self) -> None:
        a = _make_bare_adapter()
        parent_sid = a._dispatch_thread_session("100", None)
        thread_sid = a._dispatch_thread_session("200", "100")
        assert parent_sid != thread_sid

    def test_thread_session_stable(self) -> None:
        a = _make_bare_adapter()
        s1 = a._dispatch_thread_session("200", "100")
        s2 = a._dispatch_thread_session("200", "100")
        assert s1 == s2

    def test_two_threads_under_same_parent_differ(self) -> None:
        a = _make_bare_adapter()
        s1 = a._dispatch_thread_session("201", "100")
        s2 = a._dispatch_thread_session("202", "100")
        assert s1 != s2


# ---------------------------------------------------------------------------
# Adapter parent-channel lookup
# ---------------------------------------------------------------------------


class TestAdapterParentLookup:
    def test_parent_lookup_via_cache(self) -> None:
        a = _make_bare_adapter()
        parent = SimpleNamespace(type=discord.ChannelType.forum, id=500)
        thread = SimpleNamespace(
            type=discord.ChannelType.public_thread,
            id=501,
            parent=parent,
        )
        a._channel_cache["501"] = thread
        assert a._thread_parent_channel(501) == 500

    def test_text_channel_returns_none(self) -> None:
        a = _make_bare_adapter()
        ch = SimpleNamespace(type=discord.ChannelType.text, id=600)
        a._channel_cache["600"] = ch
        # Not a thread → parent is None
        assert a._thread_parent_channel(600) is None

    def test_unknown_channel_safe(self) -> None:
        a = _make_bare_adapter()
        a._client = MagicMock()
        a._client.get_channel = MagicMock(return_value=None)
        assert a._thread_parent_channel(99999) is None


class TestAutoCreateThread:
    @pytest.mark.asyncio
    async def test_auto_create_in_forum(self) -> None:
        a = _make_bare_adapter()
        new_thread = SimpleNamespace(id=7777)
        forum = MagicMock()
        forum.type = discord.ChannelType.forum
        wrapper = MagicMock()
        wrapper.thread = new_thread
        forum.create_thread = AsyncMock(return_value=wrapper)

        author = SimpleNamespace(display_name="bob")
        msg = SimpleNamespace(
            channel=forum,
            author=author,
            content="What is the meaning of life?",
        )
        new_id = await a._auto_create_thread(msg)
        assert new_id == 7777
        forum.create_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_create_failure_returns_none(self) -> None:
        a = _make_bare_adapter()
        forum = MagicMock()
        forum.type = discord.ChannelType.forum
        forum.create_thread = AsyncMock(side_effect=RuntimeError("nope"))
        msg = SimpleNamespace(
            channel=forum,
            author=SimpleNamespace(display_name="x"),
            content="long" * 100,
        )
        # Should swallow exception and return None
        out = await a._auto_create_thread(msg)
        assert out is None
