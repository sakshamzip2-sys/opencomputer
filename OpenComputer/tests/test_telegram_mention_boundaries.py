"""Tests for Telegram mention-boundary gate (PR 3a.1).

Covers entity-based @-mention detection (NEVER substring), text_mention
by user id, reply-to-bot bypass, free-response-chats bypass, wake-word
regex patterns, and the audit C4 mandate that the default config (no
``require_mention`` key) leaves group messages flowing.
"""

from __future__ import annotations

from typing import Any

import pytest

from extensions.telegram.adapter import TelegramAdapter


def _make_adapter(**config: Any) -> TelegramAdapter:
    a = TelegramAdapter({"bot_token": "test", **config})
    a._bot_id = 42
    a._bot_username = "hermes_bot"
    return a


def _msg(
    text: str = "",
    *,
    chat_type: str = "supergroup",
    chat_id: int = -100,
    entities: list[dict[str, Any]] | None = None,
    reply_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "message_id": 1,
        "from": {"id": 999},
        "chat": {"id": chat_id, "type": chat_type},
        "date": 0,
        "text": text,
    }
    if entities is not None:
        m["entities"] = entities
    if reply_to is not None:
        m["reply_to_message"] = reply_to
    return m


# ─── _message_mentions_bot: entity-based, never substring ──────────


class TestMessageMentionsBot:
    def test_mention_entity_at_start(self) -> None:
        a = _make_adapter()
        msg = _msg(
            "@hermes_bot ping",
            entities=[{"type": "mention", "offset": 0, "length": 11}],
        )
        assert a._message_mentions_bot(msg) is True

    def test_mention_entity_in_middle(self) -> None:
        a = _make_adapter()
        # "hey @hermes_bot are you there?" — the @mention spans offset=4..15
        msg = _msg(
            "hey @hermes_bot are you there?",
            entities=[{"type": "mention", "offset": 4, "length": 11}],
        )
        assert a._message_mentions_bot(msg) is True

    def test_text_mention_by_user_id(self) -> None:
        """Bots without a public username get ``text_mention`` with user obj."""
        a = _make_adapter()
        a._bot_username = None  # simulate no-username bot
        msg = _msg(
            "ping",
            entities=[
                {"type": "text_mention", "offset": 0, "length": 4,
                 "user": {"id": 42}},
            ],
        )
        assert a._message_mentions_bot(msg) is True

    def test_no_entities_returns_false(self) -> None:
        a = _make_adapter()
        msg = _msg("just chatting")
        assert a._message_mentions_bot(msg) is False

    def test_substring_at_hermes_bot_admin_rejected(self) -> None:
        """``@hermes_bot_admin`` must NOT be treated as a mention of ``@hermes_bot``."""
        a = _make_adapter()
        msg = _msg(
            "@hermes_bot_admin pls help",
            entities=[{"type": "mention", "offset": 0, "length": 17}],
        )
        # Exact-match of @username only — admin variant is a different
        # account, must be rejected.
        assert a._message_mentions_bot(msg) is False

    def test_unrelated_mention_entity_rejected(self) -> None:
        a = _make_adapter()
        msg = _msg(
            "@someone_else hi",
            entities=[{"type": "mention", "offset": 0, "length": 13}],
        )
        assert a._message_mentions_bot(msg) is False

    def test_text_mention_for_other_user_rejected(self) -> None:
        a = _make_adapter()
        msg = _msg(
            "hi",
            entities=[
                {"type": "text_mention", "offset": 0, "length": 2,
                 "user": {"id": 12345}},
            ],
        )
        assert a._message_mentions_bot(msg) is False

    def test_caption_entities_used_for_attachments(self) -> None:
        """Photo/document captions store entities under caption_entities."""
        a = _make_adapter()
        msg: dict[str, Any] = {
            "from": {"id": 1},
            "chat": {"id": -100, "type": "supergroup"},
            "caption": "@hermes_bot look at this",
            "caption_entities": [
                {"type": "mention", "offset": 0, "length": 11},
            ],
        }
        assert a._message_mentions_bot(msg) is True


# ─── _is_reply_to_bot ──────────────────────────────────────────────


class TestIsReplyToBot:
    def test_reply_to_bot_true(self) -> None:
        a = _make_adapter()
        msg = _msg("yes", reply_to={"from": {"id": 42}})
        assert a._is_reply_to_bot(msg) is True

    def test_reply_to_other_false(self) -> None:
        a = _make_adapter()
        msg = _msg("yes", reply_to={"from": {"id": 99}})
        assert a._is_reply_to_bot(msg) is False

    def test_no_reply_false(self) -> None:
        a = _make_adapter()
        msg = _msg("yes")
        assert a._is_reply_to_bot(msg) is False


# ─── _should_process_message — the gate ────────────────────────────


class TestShouldProcessMessage:
    # Default-OFF / audit C4 mandate -----------------------------------

    def test_default_config_one_to_one_passes(self) -> None:
        """No require_mention key, 1:1 chat: always pass (regression)."""
        a = _make_adapter()
        msg = _msg("hi", chat_type="private", chat_id=999)
        assert a._should_process_message(msg) is True

    def test_default_config_group_message_passes(self) -> None:
        """No require_mention key in config: groups also pass (audit C4)."""
        a = _make_adapter()
        msg = _msg("hi everyone", chat_type="supergroup", chat_id=-100)
        assert a._should_process_message(msg) is True

    def test_explicit_false_group_message_passes(self) -> None:
        a = _make_adapter(require_mention=False)
        msg = _msg("hi", chat_type="supergroup", chat_id=-100)
        assert a._should_process_message(msg) is True

    # require_mention=True ---------------------------------------------

    def test_require_mention_private_chat_still_passes(self) -> None:
        """1:1 DMs bypass the gate even when require_mention=True."""
        a = _make_adapter(require_mention=True)
        msg = _msg("hi", chat_type="private", chat_id=555)
        assert a._should_process_message(msg) is True

    def test_require_mention_group_no_mention_dropped(self) -> None:
        a = _make_adapter(require_mention=True)
        msg = _msg("hi friends", chat_type="supergroup", chat_id=-100)
        assert a._should_process_message(msg) is False

    def test_require_mention_group_with_entity_passes(self) -> None:
        a = _make_adapter(require_mention=True)
        msg = _msg(
            "@hermes_bot help",
            chat_type="supergroup",
            chat_id=-100,
            entities=[{"type": "mention", "offset": 0, "length": 11}],
        )
        assert a._should_process_message(msg) is True

    def test_require_mention_group_substring_lookalike_dropped(self) -> None:
        """``@hermes_bot_admin`` must not pass even though it contains the bot's @."""
        a = _make_adapter(require_mention=True)
        msg = _msg(
            "@hermes_bot_admin can you escalate?",
            chat_type="supergroup",
            chat_id=-100,
            entities=[{"type": "mention", "offset": 0, "length": 17}],
        )
        assert a._should_process_message(msg) is False

    # Bypass paths -----------------------------------------------------

    def test_free_response_chats_bypass(self) -> None:
        a = _make_adapter(
            require_mention=True,
            free_response_chats=[-100200300],
        )
        msg = _msg("hi", chat_type="supergroup", chat_id=-100200300)
        assert a._should_process_message(msg) is True

    def test_free_response_chats_string_id_also_works(self) -> None:
        """Config may carry chat ids as strings; gate normalises."""
        a = _make_adapter(
            require_mention=True,
            free_response_chats=["-100200300"],
        )
        msg = _msg("hi", chat_type="supergroup", chat_id=-100200300)
        assert a._should_process_message(msg) is True

    def test_reply_to_bot_bypass(self) -> None:
        a = _make_adapter(require_mention=True)
        msg = _msg(
            "yes",
            chat_type="supergroup",
            chat_id=-100,
            reply_to={"from": {"id": 42}},
        )
        assert a._should_process_message(msg) is True

    # Wake-word regex --------------------------------------------------

    def test_wake_word_pattern_matches(self) -> None:
        a = _make_adapter(
            require_mention=True,
            mention_patterns=[r"\bhey hermes\b"],
        )
        msg = _msg("hey hermes what's the weather", chat_type="supergroup", chat_id=-100)
        assert a._should_process_message(msg) is True

    def test_wake_word_pattern_case_insensitive(self) -> None:
        a = _make_adapter(
            require_mention=True,
            mention_patterns=[r"\bhey hermes\b"],
        )
        msg = _msg("HEY Hermes!", chat_type="supergroup", chat_id=-100)
        assert a._should_process_message(msg) is True

    def test_wake_word_pattern_no_match_dropped(self) -> None:
        a = _make_adapter(
            require_mention=True,
            mention_patterns=[r"\bhey hermes\b"],
        )
        msg = _msg("hi everyone", chat_type="supergroup", chat_id=-100)
        assert a._should_process_message(msg) is False

    def test_invalid_regex_logged_and_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        """A bad regex must not break adapter init or inbound delivery."""
        a = _make_adapter(
            require_mention=True,
            mention_patterns=[r"[unclosed", r"\bvalid\b"],
        )
        # Bad pattern dropped, valid one kept.
        assert len(a._mention_patterns) == 1
        msg = _msg("this is valid text", chat_type="supergroup", chat_id=-100)
        assert a._should_process_message(msg) is True


# ─── _handle_update integration: gate runs BEFORE MessageEvent ─────


class TestGateWiredIntoHandleUpdate:
    @pytest.mark.asyncio
    async def test_gate_drops_group_message_without_mention(self) -> None:
        """When require_mention=True, _handle_update must skip filtered msgs."""
        a = _make_adapter(require_mention=True)
        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]

        update = {
            "update_id": 1,
            "message": _msg(
                "hi all",
                chat_type="supergroup",
                chat_id=-100,
            ),
        }
        await a._handle_update(update)
        assert delivered == []

    @pytest.mark.asyncio
    async def test_gate_passes_mentioned_message(self) -> None:
        a = _make_adapter(require_mention=True)
        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]
        update = {
            "update_id": 2,
            "message": _msg(
                "@hermes_bot help",
                chat_type="supergroup",
                chat_id=-100,
                entities=[{"type": "mention", "offset": 0, "length": 11}],
            ),
        }
        await a._handle_update(update)
        assert len(delivered) == 1
        assert delivered[0].text == "@hermes_bot help"

    @pytest.mark.asyncio
    async def test_default_config_group_still_delivers(self) -> None:
        """Audit C4 regression: default config keeps groups working."""
        a = _make_adapter()  # no require_mention
        delivered: list[Any] = []

        async def handler(ev: Any) -> None:
            delivered.append(ev)

        a._message_handler = handler  # type: ignore[assignment]
        update = {
            "update_id": 3,
            "message": _msg("hi all", chat_type="supergroup", chat_id=-100),
        }
        await a._handle_update(update)
        assert len(delivered) == 1
