"""Wave 3 — `oc model add/list/remove` CLI."""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from opencomputer.cli import app


def test_model_add_writes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "model", "add", "groq",
            "--base-url", "https://api.groq.com/openai/v1",
            "--key-env", "GROQ_API_KEY",
            "--no-probe",
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["custom_providers"][0]["name"] == "groq"
    assert cfg["custom_providers"][0]["base_url"] == "https://api.groq.com/openai/v1"
    assert cfg["custom_providers"][0]["key_env"] == "GROQ_API_KEY"


def test_model_add_refuses_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, [
        "model", "add", "alpha", "--base-url", "http://a.local", "--no-probe",
    ])
    result = runner.invoke(app, [
        "model", "add", "alpha", "--base-url", "http://other", "--no-probe",
    ])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_model_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["model", "list"])
    assert result.exit_code == 0
    assert "no custom_providers configured" in result.output


def test_model_list_shows_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, [
        "model", "add", "alpha", "--base-url", "http://a.local",
        "--key-env", "K", "--no-probe",
    ])
    result = runner.invoke(app, ["model", "list"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "http://a.local" in result.output


def test_model_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    runner.invoke(app, [
        "model", "add", "alpha", "--base-url", "http://a", "--no-probe",
    ])
    runner.invoke(app, [
        "model", "add", "beta", "--base-url", "http://b", "--no-probe",
    ])
    result = runner.invoke(app, ["model", "remove", "alpha"])
    assert result.exit_code == 0
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    names = {p["name"] for p in cfg["custom_providers"]}
    assert names == {"beta"}


def test_model_remove_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    # Create config.yaml first by adding a different provider, then try
    # to remove a non-existent one — exercises the "name not found" path,
    # distinct from the missing-config-file path.
    runner.invoke(app, [
        "model", "add", "real-one", "--base-url", "http://x", "--no-probe",
    ])
    result = runner.invoke(app, ["model", "remove", "nonexistent"])
    assert result.exit_code == 1
    assert "no provider named" in result.output


def test_model_remove_missing_config(tmp_path, monkeypatch):
    """If no config file exists, remove fails distinctly from name-not-found."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["model", "remove", "anything"])
    assert result.exit_code == 1
    assert "no config.yaml" in result.output
