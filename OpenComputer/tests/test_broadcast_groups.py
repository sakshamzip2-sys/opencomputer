"""Tests for opencomputer.agent.broadcast_groups."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opencomputer.agent.broadcast_groups import (
    BROADCAST_GROUPS_FIELD,
    BroadcastConfig,
    BroadcastGroup,
    broadcastGroups,
    load_broadcast_config,
    parse_broadcast_config,
)


class TestBroadcastGroup:
    def test_valid(self) -> None:
        g = BroadcastGroup(key="telegram://-100", agent_ids=("a", "b"))
        assert g.key == "telegram://-100"
        assert g.agent_ids == ("a", "b")

    def test_empty_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            BroadcastGroup(key="", agent_ids=("a",))

    def test_empty_agent_ids_rejected(self) -> None:
        with pytest.raises(ValueError):
            BroadcastGroup(key="x", agent_ids=())

    def test_duplicate_agent_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            BroadcastGroup(key="x", agent_ids=("a", "a"))

    def test_non_string_agent_rejected(self) -> None:
        with pytest.raises(ValueError):
            BroadcastGroup(key="x", agent_ids=("a", ""))


class TestParse:
    def test_empty(self) -> None:
        assert parse_broadcast_config({}).groups == {}
        assert parse_broadcast_config(None).groups == {}
        assert parse_broadcast_config("not-a-dict").groups == {}

    def test_simple_parse(self) -> None:
        raw = {"telegram://-1001": ["work", "research"]}
        cfg = parse_broadcast_config(raw)
        g = cfg.lookup("telegram://-1001")
        assert g is not None
        assert g.agent_ids == ("work", "research")

    def test_dedupes_agent_ids(self) -> None:
        raw = {"k": ["a", "a", "b"]}
        cfg = parse_broadcast_config(raw)
        assert cfg.groups["k"].agent_ids == ("a", "b")

    def test_skips_non_string_keys(self) -> None:
        raw = {42: ["a"], "valid": ["b"]}
        cfg = parse_broadcast_config(raw)
        assert cfg.keys() == ["valid"]

    def test_skips_non_list_values(self) -> None:
        raw = {"k1": ["a"], "k2": "not-a-list"}
        cfg = parse_broadcast_config(raw)
        assert cfg.keys() == ["k1"]

    def test_skips_empty_agent_lists(self) -> None:
        raw = {"k1": ["a"], "k2": [], "k3": ["", None]}
        cfg = parse_broadcast_config(raw)
        # k2 has no entries; k3 has no valid entries.
        assert cfg.keys() == ["k1"]


class TestLookup:
    def test_lookup_for_returns_match(self) -> None:
        cfg = parse_broadcast_config({"telegram://-1001": ["a"]})
        assert cfg.lookup_for("telegram", "-1001") is not None

    def test_lookup_for_no_match(self) -> None:
        cfg = parse_broadcast_config({"telegram://-1001": ["a"]})
        assert cfg.lookup_for("discord", "-1001") is None
        assert cfg.lookup_for("telegram", "-9999") is None

    def test_lookup_for_invalid_args(self) -> None:
        cfg = parse_broadcast_config({"k": ["a"]})
        assert cfg.lookup_for("", "x") is None
        assert cfg.lookup_for("x", "") is None
        assert cfg.lookup_for(None, "x") is None  # type: ignore[arg-type]


class TestLoad:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_broadcast_config(tmp_path / "missing").groups == {}
        assert load_broadcast_config(None).groups == {}

    def test_load_from_file(self, tmp_path: Path) -> None:
        p = tmp_path / "broadcast.yaml"
        p.write_text(yaml.safe_dump({"k": ["a", "b"]}))
        cfg = load_broadcast_config(p)
        assert cfg.keys() == ["k"]
        assert cfg.groups["k"].agent_ids == ("a", "b")

    def test_load_with_wrapper_key(self, tmp_path: Path) -> None:
        """``broadcast.yaml`` may use either top-level groups or wrap them."""
        p = tmp_path / "broadcast.yaml"
        p.write_text(yaml.safe_dump({"broadcast_groups": {"k": ["x"]}}))
        assert load_broadcast_config(p).keys() == ["k"]

    def test_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "broadcast.yaml"
        p.write_text("not: : : valid:")
        assert load_broadcast_config(p).groups == {}


class TestParityNames:
    def test_field_constant(self) -> None:
        assert BROADCAST_GROUPS_FIELD == "broadcast_groups"

    def test_camelcase_alias_exists(self) -> None:
        # OpenClaw spec uses camelCase; we expose both.
        assert broadcastGroups == BROADCAST_GROUPS_FIELD
