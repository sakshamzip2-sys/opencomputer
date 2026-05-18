"""Tests for the ``oc skin`` CLI (best-of-three Recipe 4, Part C).

The skin engine itself is covered elsewhere; this exercises only the
new top-level CLI: ``list`` marks the active skin, ``set`` validates +
persists, ``preview`` renders a palette.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_skin import skin_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path: Path, monkeypatch) -> None:
    """Point the profile home at a tmp dir so ``set`` never writes the
    real ~/.opencomputer/config.yaml.

    ``opencomputer.agent.config._home`` (which ``cli_skin._config_path``
    calls) resolves ``OPENCOMPUTER_HOME`` — not ``OPENCOMPUTER_HOME_ROOT``
    — so that is the var the isolation must set.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))


def test_list_shows_builtins_and_marks_default() -> None:
    result = runner.invoke(skin_app, ["list"])
    assert result.exit_code == 0
    assert "default" in result.stdout
    assert "mono" in result.stdout
    assert "daylight" in result.stdout
    assert "active skin" in result.stdout


def test_set_valid_skin_persists_and_reads_back() -> None:
    result = runner.invoke(skin_app, ["set", "mono"])
    assert result.exit_code == 0
    assert "skin set to" in result.stdout
    # list now marks mono as active
    listing = runner.invoke(skin_app, ["list"])
    assert "mono" in listing.stdout
    lines = [ln for ln in listing.stdout.splitlines() if "mono" in ln]
    assert any("●" in ln for ln in lines)


def test_set_unknown_skin_exits_nonzero_and_does_not_persist() -> None:
    result = runner.invoke(skin_app, ["set", "definitely-not-a-skin"])
    assert result.exit_code == 1
    assert "unknown skin" in result.stdout
    # active skin is still default
    listing = runner.invoke(skin_app, ["list"])
    assert "active skin" in listing.stdout
    assert "definitely-not-a-skin" not in listing.stdout


def test_set_is_case_insensitive() -> None:
    result = runner.invoke(skin_app, ["set", "MONO"])
    assert result.exit_code == 0


def test_preview_renders_palette() -> None:
    result = runner.invoke(skin_app, ["preview", "default"])
    assert result.exit_code == 0
    assert "Skin preview" in result.stdout
    # default.yaml declares a banner_border token
    assert "banner_border" in result.stdout


def test_preview_defaults_to_active_skin() -> None:
    runner.invoke(skin_app, ["set", "mono"])
    result = runner.invoke(skin_app, ["preview"])
    assert result.exit_code == 0
    assert "mono" in result.stdout
