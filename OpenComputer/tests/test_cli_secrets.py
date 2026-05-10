"""Smoke tests for ``oc secrets`` CLI surface.

Uses Typer's :class:`CliRunner` so we exercise argument parsing +
exit codes without spawning a real subprocess. The deeper provider
behaviour is covered by ``test_secrets_provider_chain.py``; these
tests just pin the user-facing wiring.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_secrets import secrets_app

_runner = CliRunner()


def test_audit_clean_file_returns_zero(tmp_path: Path):
    safe = tmp_path / "config.yaml"
    safe.write_text("model:\n  name: claude-opus\n")
    result = _runner.invoke(secrets_app, ["audit", str(safe)])
    assert result.exit_code == 0, result.output
    assert "No findings" in result.output


def test_audit_plaintext_returns_nonzero(tmp_path: Path):
    bad = tmp_path / "config.yaml"
    bad.write_text("anthropic:\n  api_key: sk-ant-totally-real-key-shhh\n")
    result = _runner.invoke(secrets_app, ["audit", str(bad)])
    assert result.exit_code == 1, result.output
    assert "plaintext" in result.output.lower()


def test_audit_json_output(tmp_path: Path):
    bad = tmp_path / "config.yaml"
    bad.write_text("anthropic:\n  api_key: sk-ant-totally-real-key-shhh\n")
    result = _runner.invoke(secrets_app, ["audit", str(bad), "--json"])
    # Plaintext findings → exit 1 even in JSON mode.
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert any(f["kind"] == "plaintext_secret" for f in payload)


def test_list_handles_no_specs(tmp_path: Path, monkeypatch):
    # No secrets.json present → graceful empty output.
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    result = _runner.invoke(secrets_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "No specs configured" in result.output


def test_list_json_with_specs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    (tmp_path / "secrets.json").write_text(json.dumps({
        "secrets": [
            {"id": "x", "source": "env", "lookup": "OC_TEST_X"},
        ],
    }))
    result = _runner.invoke(secrets_app, ["list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == [
        {"id": "x", "source": "env", "lookup": "OC_TEST_X", "provider_name": "default"},
    ]


def test_resolve_shows_length_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OC_TEST_VAL", "secret-value-1234")
    (tmp_path / "secrets.json").write_text(json.dumps({
        "secrets": [
            {"id": "x", "source": "env", "lookup": "OC_TEST_VAL"},
        ],
    }))
    result = _runner.invoke(secrets_app, ["resolve", "x"])
    assert result.exit_code == 0, result.output
    # Length printed; value not.
    assert "length=17" in result.output
    assert "secret-value-1234" not in result.output


def test_resolve_show_prints_value(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OC_TEST_VAL", "shown-val")
    (tmp_path / "secrets.json").write_text(json.dumps({
        "secrets": [
            {"id": "x", "source": "env", "lookup": "OC_TEST_VAL"},
        ],
    }))
    result = _runner.invoke(secrets_app, ["resolve", "x", "--show"])
    assert result.exit_code == 0, result.output
    assert "shown-val" in result.output


def test_resolve_unknown_id_exits_nonzero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OC_TEST_VAL", "v")
    (tmp_path / "secrets.json").write_text(json.dumps({
        "secrets": [
            {"id": "x", "source": "env", "lookup": "OC_TEST_VAL"},
        ],
    }))
    result = _runner.invoke(secrets_app, ["resolve", "nope"])
    assert result.exit_code == 1, result.output


def test_resolve_load_failure_exits_with_code_2(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("OC_DOES_NOT_EXIST", raising=False)
    (tmp_path / "secrets.json").write_text(json.dumps({
        "secrets": [
            {"id": "x", "source": "env", "lookup": "OC_DOES_NOT_EXIST"},
        ],
    }))
    result = _runner.invoke(secrets_app, ["resolve", "x"])
    assert result.exit_code == 2, result.output
    assert "Registry load failed" in result.output
