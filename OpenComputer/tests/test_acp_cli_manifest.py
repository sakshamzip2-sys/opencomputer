"""T63 — `oc acp manifest` emits agent.json for IDE registration."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from opencomputer.cli import app


def test_acp_manifest_prints_agent_json():
    runner = CliRunner()
    result = runner.invoke(app, ["acp", "manifest"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["name"] == "opencomputer"
    assert payload["displayName"]
    assert payload["version"]
    # IDEs spawn the agent via this command + args.
    assert payload["command"] == "oc"
    assert payload["args"] == ["acp", "serve"]
    assert payload["transport"] == "stdio"
    assert payload["protocolVersion"]


def test_acp_manifest_write_to_file(tmp_path):
    runner = CliRunner()
    target = tmp_path / "agent.json"
    result = runner.invoke(app, ["acp", "manifest", "--write", str(target)])
    assert result.exit_code == 0, result.stdout
    assert target.exists()
    payload = json.loads(target.read_text())
    assert payload["name"] == "opencomputer"


def test_acp_manifest_includes_capabilities():
    runner = CliRunner()
    result = runner.invoke(app, ["acp", "manifest"])
    payload = json.loads(result.stdout)
    caps = payload["capabilities"]
    assert caps["streaming"] is True
    assert caps["cancellation"] is True
    assert caps["toolset"] is True


def test_acp_serve_subcommand_exists():
    """Confirms `oc acp serve` is a registered subcommand."""
    runner = CliRunner()
    result = runner.invoke(app, ["acp", "--help"])
    assert result.exit_code == 0
    assert "serve" in result.stdout
    assert "manifest" in result.stdout
