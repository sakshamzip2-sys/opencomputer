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
