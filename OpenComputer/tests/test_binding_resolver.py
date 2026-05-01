"""BindingResolver — match precedence + bindings.yaml schema.

Phase 3 Task 3.1: schema-only tests. Resolver tests added by Task 3.2.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
    load_bindings,
)
from opencomputer.gateway.binding_resolver import BindingResolver
from plugin_sdk.core import MessageEvent, Platform


def test_load_empty_returns_default_only(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text("default_profile: default\nbindings: []\n")
    cfg = load_bindings(cfg_path)
    assert cfg.default_profile == "default"
    assert cfg.bindings == ()


def test_load_with_one_binding(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: home\n"
        "bindings:\n"
        "  - match: { platform: telegram, chat_id: \"123\" }\n"
        "    profile: coding\n"
        "    priority: 100\n"
    )
    cfg = load_bindings(cfg_path)
    assert cfg.default_profile == "home"
    assert len(cfg.bindings) == 1
    b = cfg.bindings[0]
    assert b.profile == "coding"
    assert b.priority == 100
    assert b.match.platform == "telegram"
    assert b.match.chat_id == "123"
    assert b.match.peer_id is None


def test_load_missing_file_returns_default(tmp_path: Path) -> None:
    """Missing file → empty config; the gateway boots with default-only routing."""
    cfg = load_bindings(tmp_path / "no-such-file.yaml")
    assert cfg.default_profile == "default"
    assert cfg.bindings == ()


def test_load_malformed_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text("default_profile: 123\n")  # wrong type
    with pytest.raises(ValueError):
        load_bindings(cfg_path)


def test_load_unknown_top_level_field_raises(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\nbindings: []\nbogus_field: 42\n"
    )
    with pytest.raises(ValueError) as exc:
        load_bindings(cfg_path)
    assert "bogus_field" in str(exc.value)


def test_load_match_with_all_fields(tmp_path: Path) -> None:
    """All 5 match fields can be set together (AND semantics)."""
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match:\n"
        "      platform: telegram\n"
        "      chat_id: \"123\"\n"
        "      group_id: \"g1\"\n"
        "      peer_id: \"p1\"\n"
        "      account_id: \"a1\"\n"
        "    profile: x\n"
    )
    cfg = load_bindings(cfg_path)
    b = cfg.bindings[0]
    assert b.match.platform == "telegram"
    assert b.match.chat_id == "123"
    assert b.match.group_id == "g1"
    assert b.match.peer_id == "p1"
    assert b.match.account_id == "a1"


def test_load_unknown_match_key_raises(tmp_path: Path) -> None:
    """Unknown match field in bindings[i].match → ValueError."""
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram, bogus_match_key: x }\n"
        "    profile: x\n"
    )
    with pytest.raises(ValueError) as exc:
        load_bindings(cfg_path)
    assert "bogus_match_key" in str(exc.value)


def test_load_priority_default_zero(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram }\n"
        "    profile: x\n"
    )
    cfg = load_bindings(cfg_path)
    assert cfg.bindings[0].priority == 0


def test_load_chat_id_int_in_yaml_coerced_to_str(tmp_path: Path) -> None:
    """YAML may parse chat_id: 12345 as int; loader coerces to str for matching."""
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram, chat_id: 12345 }\n"
        "    profile: x\n"
    )
    cfg = load_bindings(cfg_path)
    assert cfg.bindings[0].match.chat_id == "12345"
    assert isinstance(cfg.bindings[0].match.chat_id, str)


# ---- BindingResolver tests (Task 3.2) ----


def _ev(**kwargs) -> MessageEvent:
    """Minimal MessageEvent factory for resolver tests."""
    return MessageEvent(
        platform=kwargs.pop("platform", Platform.TELEGRAM),
        chat_id=kwargs.pop("chat_id", "0"),
        user_id=kwargs.pop("user_id", "u0"),
        text=kwargs.pop("text", ""),
        timestamp=kwargs.pop("timestamp", 0.0),
        attachments=[],
        metadata=kwargs.pop("metadata", {}),
    )


def test_resolver_default_when_no_bindings() -> None:
    cfg = BindingsConfig(default_profile="home", bindings=())
    r = BindingResolver(cfg)
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="123")) == "home"


def test_resolver_chat_id_match() -> None:
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(match=BindingMatch(chat_id="123"), profile="coding"),
        ),
    )
    r = BindingResolver(cfg)
    assert r.resolve(_ev(chat_id="123")) == "coding"
    assert r.resolve(_ev(chat_id="999")) == "home"


def test_resolver_chat_beats_platform_at_same_priority() -> None:
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(match=BindingMatch(platform="telegram"), profile="personal", priority=10),
            Binding(match=BindingMatch(chat_id="123"), profile="coding", priority=10),
        ),
    )
    r = BindingResolver(cfg)
    # chat_id is more specific than platform-only — wins regardless of order
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="123")) == "coding"
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="999")) == "personal"


def test_resolver_priority_breaks_tie_within_same_specificity() -> None:
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(match=BindingMatch(chat_id="123"), profile="lower", priority=1),
            Binding(match=BindingMatch(chat_id="123"), profile="higher", priority=99),
        ),
    )
    r = BindingResolver(cfg)
    assert r.resolve(_ev(chat_id="123")) == "higher"


def test_resolver_and_semantics_in_match() -> None:
    """match: { platform: telegram, chat_id: '123' } requires BOTH."""
    cfg = BindingsConfig(
        default_profile="home",
        bindings=(
            Binding(
                match=BindingMatch(platform="telegram", chat_id="123"),
                profile="coding",
            ),
        ),
    )
    r = BindingResolver(cfg)
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="123")) == "coding"
    assert r.resolve(_ev(platform=Platform.DISCORD, chat_id="123")) == "home"
    assert r.resolve(_ev(platform=Platform.TELEGRAM, chat_id="999")) == "home"


def test_resolver_warns_on_unsupported_field(caplog) -> None:
    """Pass-2 F3 fix: a binding referencing peer_id/group_id/account_id
    on a platform that doesn't surface those fields in v1 logs an ERROR
    at load time. The binding will silently never match without the warning."""
    import logging
    cfg = BindingsConfig(
        default_profile="default",
        bindings=(Binding(
            match=BindingMatch(platform="telegram", peer_id="123"),
            profile="x",
        ),),
    )
    with caplog.at_level(logging.ERROR, logger="opencomputer.gateway.binding_resolver"):
        BindingResolver(cfg)
    assert any("peer_id" in r.message for r in caplog.records), (
        f"Expected ERROR log mentioning 'peer_id'; got {[r.message for r in caplog.records]}"
    )


def test_resolver_no_warn_for_supported_fields(caplog) -> None:
    """A binding using only chat_id + platform (both supported in v1) does NOT warn."""
    import logging
    cfg = BindingsConfig(
        default_profile="default",
        bindings=(Binding(
            match=BindingMatch(platform="telegram", chat_id="123"),
            profile="x",
        ),),
    )
    with caplog.at_level(logging.ERROR, logger="opencomputer.gateway.binding_resolver"):
        BindingResolver(cfg)
    # No ERROR records expected
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert not error_records, f"Unexpected ERROR records: {[r.message for r in error_records]}"
