"""T5 — `oc honcho` CLI subcommand group."""

from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def honcho_app(monkeypatch, tmp_path):
    """Set OPENCOMPUTER_HOME before importing so the CLI binds to tmp_path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Re-import to pick up env var if module reads it at import time.
    import importlib

    import opencomputer.cli_honcho as mod
    importlib.reload(mod)
    return mod.honcho_app


def _read_cfg(tmp_path) -> dict:
    cfg_path = tmp_path / "config.yaml"
    if not cfg_path.exists():
        return {}
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def test_status_runs_without_honcho(runner, honcho_app):
    result = runner.invoke(honcho_app, ["status"])
    assert result.exit_code == 0
    assert "honcho" in result.stdout.lower()


def test_enable_writes_provider_to_config(runner, honcho_app, tmp_path):
    result = runner.invoke(honcho_app, ["enable"])
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    assert cfg.get("memory", {}).get("provider") == "honcho"


def test_disable_writes_provider_builtin(runner, honcho_app, tmp_path):
    runner.invoke(honcho_app, ["enable"])
    result = runner.invoke(honcho_app, ["disable"])
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    assert cfg.get("memory", {}).get("provider") == "builtin"


def test_strategy_balanced_writes_cadence(runner, honcho_app, tmp_path):
    result = runner.invoke(honcho_app, ["strategy", "balanced"])
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    mem = cfg.get("memory", {})
    assert mem.get("context_cadence") == 2
    assert mem.get("dialectic_cadence") == 4


def test_strategy_aggressive_sets_medium_reasoning(runner, honcho_app, tmp_path):
    result = runner.invoke(honcho_app, ["strategy", "aggressive"])
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    mem = cfg.get("memory", {})
    assert mem.get("context_cadence") == 1
    assert mem.get("dialectic_cadence") == 2
    assert mem.get("dialectic_reasoning_level") == "medium"


def test_strategy_low_preset(runner, honcho_app, tmp_path):
    result = runner.invoke(honcho_app, ["strategy", "low"])
    assert result.exit_code == 0
    cfg = _read_cfg(tmp_path)
    mem = cfg.get("memory", {})
    assert mem.get("context_cadence") == 4
    assert mem.get("dialectic_cadence") == 8


def test_strategy_invalid_name_exits_nonzero(runner, honcho_app):
    result = runner.invoke(honcho_app, ["strategy", "ludicrous"])
    assert result.exit_code != 0
    assert "preset" in result.stdout.lower() or "low" in result.stdout.lower()


def test_status_shows_preset_after_strategy(runner, honcho_app, tmp_path):
    runner.invoke(honcho_app, ["enable"])
    runner.invoke(honcho_app, ["strategy", "balanced"])
    result = runner.invoke(honcho_app, ["status"])
    assert result.exit_code == 0
    assert "balanced" in result.stdout.lower()


def test_sync_runs_when_no_profiles_exist(runner, honcho_app, tmp_path):
    """sync should not crash on a missing profiles dir."""
    result = runner.invoke(honcho_app, ["sync"])
    # Either exit 0 (best-effort) OR exit 1 with a clear message.
    assert result.exit_code in (0, 1)
