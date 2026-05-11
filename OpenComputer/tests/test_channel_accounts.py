"""Tests for plugin_sdk.channel_accounts — multi-account channel SDK.

Covers:
* Valid round-trip parse.
* Missing / empty / wrong-type → graceful empty config.
* Validation rejects malformed account ids.
* MultiAccountChannel mixin abstract requirements.
"""

from __future__ import annotations

import pytest

from plugin_sdk.channel_accounts import (
    ACCOUNTS_FIELD,
    ChannelAccount,
    ChannelAccountsConfig,
    MultiAccountChannel,
)


class TestChannelAccountsConfig:
    def test_empty_default(self) -> None:
        c = ChannelAccountsConfig()
        assert c.accounts == {}
        assert c.ids() == []

    def test_from_raw_dict(self) -> None:
        raw = {
            "personal-bot": {"bot_token": "abc123"},
            "work-bot": {"bot_token": "def456"},
        }
        c = ChannelAccountsConfig.from_raw(raw)
        assert c.ids() == ["personal-bot", "work-bot"]
        acc = c.get("personal-bot")
        assert acc is not None
        assert acc.params == {"bot_token": "abc123"}

    def test_from_raw_none(self) -> None:
        assert ChannelAccountsConfig.from_raw(None).accounts == {}

    def test_from_raw_non_dict(self) -> None:
        assert ChannelAccountsConfig.from_raw([]).accounts == {}
        assert ChannelAccountsConfig.from_raw("string").accounts == {}

    def test_from_raw_skips_malformed_entries(self) -> None:
        raw = {
            "valid": {"k": "v"},
            "": {"k": "v"},        # empty key skipped
            "no-dict": "string",   # non-dict value skipped
            42: {"k": "v"},        # non-str key skipped
        }
        c = ChannelAccountsConfig.from_raw(raw)
        assert c.ids() == ["valid"]

    def test_get_missing(self) -> None:
        c = ChannelAccountsConfig.from_raw({"a": {}})
        assert c.get("missing") is None

    def test_post_init_rejects_id_mismatch(self) -> None:
        with pytest.raises(ValueError, match="id mismatch"):
            ChannelAccountsConfig(
                accounts={"a": ChannelAccount(id="b", params={})}
            )

    def test_post_init_rejects_empty_key(self) -> None:
        with pytest.raises(ValueError):
            ChannelAccountsConfig(
                accounts={"": ChannelAccount(id="", params={})}
            )

    def test_field_name_constant(self) -> None:
        # The schema field name must match OpenClaw / parity-doctor spec.
        assert ACCOUNTS_FIELD == "accounts"


class TestMultiAccountChannel:
    def test_abstract_methods_enforced(self) -> None:
        # Cannot instantiate abstract base.
        with pytest.raises(TypeError):
            MultiAccountChannel()  # type: ignore[abstract]

    def test_subclass_with_resolve_works(self) -> None:
        class TG(MultiAccountChannel):
            def resolve_account_id(self, raw_event: object) -> str | None:
                return raw_event.get("bot") if isinstance(raw_event, dict) else None

        tg = TG()
        assert tg.resolve_account_id({"bot": "work-bot"}) == "work-bot"
        assert tg.resolve_account_id("not-a-dict") is None
        # Default configured_accounts is empty.
        assert tg.configured_accounts().accounts == {}

    def test_subclass_can_override_configured_accounts(self) -> None:
        class TG(MultiAccountChannel):
            def resolve_account_id(self, raw_event: object) -> str | None:
                return None

            def configured_accounts(self) -> ChannelAccountsConfig:
                return ChannelAccountsConfig.from_raw({"x": {}})

        assert TG().configured_accounts().ids() == ["x"]
