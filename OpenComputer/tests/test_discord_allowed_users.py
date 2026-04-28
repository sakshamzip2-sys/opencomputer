"""Tests for Discord allowed_users / allowed_roles allowlist (PR 3b.1).

The allowlist uses OR semantics: when either ``allowed_users`` or
``allowed_roles`` is configured, an author passes if they appear in
the user list OR carry at least one role from the role list. Empty
both ⇒ allowlist is open.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "discord_adapter_pr3b1_allowlist",
        Path(__file__).resolve().parent.parent / "extensions" / "discord" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DiscordAdapter


def _make_adapter(
    *,
    allowed_users: list[Any] | None = None,
    allowed_roles: list[Any] | None = None,
):
    DiscordAdapter = _load_adapter()
    a = object.__new__(DiscordAdapter)
    a.config = {}
    a.token = "fake"
    bot_user = SimpleNamespace(id=12345, bot=True)
    fake_client = MagicMock()
    fake_client.user = bot_user
    bot_user.mentioned_in = lambda msg: any(  # type: ignore[attr-defined]
        getattr(u, "id", None) == 12345 for u in (msg.mentions or [])
    )
    a._client = fake_client
    a._bot_user_id = 12345
    a._channel_cache = {}
    a._client_task = None
    a._ready_event = MagicMock()
    a._require_mention = False
    a._allowed_users = {str(u) for u in (allowed_users or [])}
    a._allowed_roles = {str(r) for r in (allowed_roles or [])}
    a._allow_bots = "none"
    return a


def _make_msg(*, author_id: int, role_ids: list[int] | None = None):
    author = SimpleNamespace(
        id=author_id,
        bot=False,
        roles=[SimpleNamespace(id=r) for r in (role_ids or [])],
    )
    return SimpleNamespace(
        author=author,
        content="hi",
        guild=SimpleNamespace(id=42),
        channel=SimpleNamespace(id=100),
        id=7777,
        mentions=[],
    )


class TestEmptyAllowlists:
    def test_no_lists_configured_open(self) -> None:
        a = _make_adapter()
        assert a._should_process(_make_msg(author_id=42)) is True


class TestUserAllowlist:
    def test_user_in_allowlist_passes(self) -> None:
        a = _make_adapter(allowed_users=[42, 999])
        assert a._should_process(_make_msg(author_id=42)) is True

    def test_user_not_in_allowlist_blocked(self) -> None:
        a = _make_adapter(allowed_users=[42, 999])
        assert a._should_process(_make_msg(author_id=11)) is False

    def test_string_id_in_config_matches_int_author_id(self) -> None:
        """Config IDs may be JSON strings; must compare-as-string."""
        a = _make_adapter(allowed_users=["42"])
        assert a._should_process(_make_msg(author_id=42)) is True


class TestRoleAllowlist:
    def test_role_in_allowlist_passes(self) -> None:
        a = _make_adapter(allowed_roles=[7, 8])
        msg = _make_msg(author_id=42, role_ids=[7])
        assert a._should_process(msg) is True

    def test_no_matching_role_blocked(self) -> None:
        a = _make_adapter(allowed_roles=[7, 8])
        msg = _make_msg(author_id=42, role_ids=[100])
        assert a._should_process(msg) is False

    def test_no_roles_at_all_blocked(self) -> None:
        a = _make_adapter(allowed_roles=[7])
        msg = _make_msg(author_id=42, role_ids=[])
        assert a._should_process(msg) is False


class TestOrSemantics:
    def test_user_match_only_passes(self) -> None:
        """Both lists set; user matches but has no allowed roles → still passes."""
        a = _make_adapter(allowed_users=[42], allowed_roles=[7])
        msg = _make_msg(author_id=42, role_ids=[100])
        assert a._should_process(msg) is True

    def test_role_match_only_passes(self) -> None:
        """User isn't in the user list but has an allowed role → passes."""
        a = _make_adapter(allowed_users=[42], allowed_roles=[7])
        msg = _make_msg(author_id=999, role_ids=[7])
        assert a._should_process(msg) is True

    def test_neither_matches_blocked(self) -> None:
        a = _make_adapter(allowed_users=[42], allowed_roles=[7])
        msg = _make_msg(author_id=999, role_ids=[100])
        assert a._should_process(msg) is False
