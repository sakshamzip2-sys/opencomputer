"""Tests for Telegram chat-allowlist gate (Wave 6.A).

Hermes-port (591aa159a). Allowlist gate at the front of ``_handle_update``
that drops messages from disallowed chats before the mention gate runs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _load_adapter():
    p = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "telegram"
        / "adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_telegram_allowlist", str(p)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def TelegramAdapter(scope="module"):  # noqa: N802
    return _load_adapter().TelegramAdapter


def _stub(cls, *, allowed=None, private_require=False):
    a = cls.__new__(cls)
    a._allowed_chats = set(allowed or [])
    a._private_chats_require_allow = private_require
    return a


def test_empty_allowlist_passes_all(TelegramAdapter):  # noqa: N803
    a = _stub(TelegramAdapter)
    assert a._is_chat_allowed({"chat": {"id": -100, "type": "supergroup"}}) is True
    assert a._is_chat_allowed({"chat": {"id": 1, "type": "private"}}) is True


def test_non_empty_allows_listed_only(TelegramAdapter):  # noqa: N803
    a = _stub(TelegramAdapter, allowed=["-100123"])
    assert a._is_chat_allowed({"chat": {"id": -100123, "type": "supergroup"}}) is True
    assert a._is_chat_allowed({"chat": {"id": -200, "type": "supergroup"}}) is False


def test_private_chat_passes_when_not_required(TelegramAdapter):  # noqa: N803
    a = _stub(TelegramAdapter, allowed=["-100123"])
    # private chat 555 not in allowlist, but private bypass = True (default)
    assert a._is_chat_allowed({"chat": {"id": 555, "type": "private"}}) is True


def test_private_chat_gated_when_required(TelegramAdapter):  # noqa: N803
    a = _stub(TelegramAdapter, allowed=["-100123"], private_require=True)
    assert a._is_chat_allowed({"chat": {"id": 555, "type": "private"}}) is False
    assert a._is_chat_allowed({"chat": {"id": -100123, "type": "private"}}) is True


def test_forum_thread_inherits_parent_chat_decision(TelegramAdapter):  # noqa: N803
    """Telegram forum threads share the parent ``chat.id``; allowlist
    checks against parent, so allow/deny applies forum-wide."""
    a = _stub(TelegramAdapter, allowed=["-100forum"])
    # message in a forum thread carries the parent chat.id, plus message_thread_id
    msg = {
        "chat": {"id": -100, "type": "supergroup"},
        "message_thread_id": 42,
    }
    a2 = _stub(TelegramAdapter, allowed=["-100"])
    assert a2._is_chat_allowed(msg) is True
    # different chat: rejected even with same thread id
    msg_other = {"chat": {"id": -999, "type": "supergroup"}, "message_thread_id": 42}
    assert a2._is_chat_allowed(msg_other) is False


def test_string_and_int_ids_both_match(TelegramAdapter):  # noqa: N803
    """Telegram delivers ids as ints; allowlist stores strings; convert."""
    a = _stub(TelegramAdapter, allowed=["-100123"])
    assert a._is_chat_allowed({"chat": {"id": -100123, "type": "group"}}) is True


def test_missing_chat_field_treated_as_disallowed(TelegramAdapter):  # noqa: N803
    a = _stub(TelegramAdapter, allowed=["-100"])
    # No chat object at all → falls into private-bypass path (chat_id="" not in
    # allowlist, type defaults to private, private_require=False → True).
    # If private_require=True: rejected.
    assert a._is_chat_allowed({}) is True  # default-private bypass
    a2 = _stub(TelegramAdapter, allowed=["-100"], private_require=True)
    assert a2._is_chat_allowed({}) is False
