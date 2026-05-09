"""v1.1 plan-3 M10.4 — `oc routing test/list` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_routing import routing_app


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


@pytest.fixture
def routed_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop a routing-equipped config.yaml and point load_config at it."""
    cfg_path = _write_config(
        tmp_path,
        """
routing:
  rules:
    - match: {platform: slack, channel: "#security-alerts"}
      agent: security-reviewer
    - match: {platform: telegram, peer: "12345"}
      agent: executive
      profile: work
    - match: {platform: discord, guild: myguild, role: admin}
      agent: admin-only
    - match: {platform: discord, guild: myguild}
      agent: guild-default
  default:
    agent: fallback
""",
    )
    # Steer load_config() at our temp file by patching the discovery.
    import opencomputer.cli_routing as cli_routing

    monkeypatch.setattr(
        cli_routing, "load_config", lambda *_args, **_kw: _real_load(cfg_path)
    )
    return cfg_path


def _real_load(path: Path):
    from opencomputer.agent.config_store import load_config

    return load_config(path)


# ─── oc routing list ─────────────────────────────────────────────────────


def test_list_text_output(routed_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(routing_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "4" in result.output  # 4 rules
    assert "security-reviewer" in result.output
    assert "executive" in result.output
    assert "admin-only" in result.output
    assert "guild-default" in result.output
    assert "fallback" in result.output


def test_list_json_output(routed_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(routing_app, ["list", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["rules"]) == 4
    agents = {r["agent"] for r in payload["rules"]}
    assert agents == {"security-reviewer", "executive", "admin-only", "guild-default"}
    assert payload["default"]["agent"] == "fallback"


def test_list_no_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = _write_config(tmp_path, "model:\n  model: claude-opus-4-7\n")
    import opencomputer.cli_routing as cli_routing

    monkeypatch.setattr(
        cli_routing, "load_config", lambda *_args, **_kw: _real_load(cfg_path)
    )
    runner = CliRunner()
    result = runner.invoke(routing_app, ["list"])
    assert result.exit_code == 0
    assert "No routing rules configured" in result.output


# ─── oc routing test ─────────────────────────────────────────────────────


def test_test_matches_security_channel(routed_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        routing_app,
        ["test", "slack", "U123", "--channel", "security-alerts"],
    )
    assert result.exit_code == 0, result.output
    assert "security-reviewer" in result.output
    assert "Matched rule" in result.output


def test_test_matches_admin_role(routed_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        routing_app,
        ["test", "discord", "U123", "--guild", "myguild", "--role", "admin"],
    )
    assert result.exit_code == 0, result.output
    assert "admin-only" in result.output


def test_test_falls_through_to_default(routed_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(routing_app, ["test", "matrix", "U999"])
    assert result.exit_code == 0, result.output
    assert "DEFAULT" in result.output
    assert "fallback" in result.output


def test_test_json_output(routed_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        routing_app,
        ["test", "telegram", "12345", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agent"] == "executive"
    assert payload["profile"] == "work"
    assert payload["matched_default"] is False
    assert payload["rule"]["agent"] == "executive"


def test_test_channel_with_hash_normalizes(routed_config: Path) -> None:
    """`--channel #security-alerts` matches rules written with `channel: security-alerts`."""
    runner = CliRunner()
    result = runner.invoke(
        routing_app,
        ["test", "slack", "U123", "--channel", "#security-alerts"],
    )
    assert result.exit_code == 0, result.output
    assert "security-reviewer" in result.output
