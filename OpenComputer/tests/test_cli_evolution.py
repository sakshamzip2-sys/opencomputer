"""``oc evolution {status,tune,reset}`` CLI coverage.

Uses Typer's :class:`CliRunner` so the tests exercise the actual Typer
command-line surface (arg parsing, exit codes, stdout content) the user
hits when typing ``oc evolution status`` in a terminal.

Each test sets ``OPENCOMPUTER_PROFILE_HOME`` to a tmp dir so the CLI
operates against an isolated profile state file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.evolution_orchestrator import (
    DEFAULT_TUNING,
    SCHEMA_VERSION,
    load_tuning,
)
from opencomputer.cli_evolution import evolution_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_profile(tmp_path, monkeypatch):
    """Point the CLI at a temp profile so tests don't touch real state."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    (tmp_path / "skills").mkdir(exist_ok=True)
    yield tmp_path


# ─── status ──────────────────────────────────────────────────────────


def test_status_with_no_state_shows_defaults(_isolate_profile):
    """No tuning file → defaults rendered, decisions=0, never recompute."""
    result = runner.invoke(evolution_app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Evolution Tuning" in result.output
    # The default confidence threshold is 70.
    assert "70" in result.output
    assert "Decisions observed: 0" in result.output
    assert "never" in result.output


def test_status_with_persisted_tuning_shows_current_values(_isolate_profile):
    """A populated tuning file reflects in the status output."""
    (_isolate_profile / "skills" / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "confidence_threshold": 85,
                "dreaming_v2_score_threshold": 0.75,
                "dreaming_v2_min_recall": 3,
                "decisions_observed": 42,
                "last_recompute_ts": 1700000000.0,
            }
        )
    )
    result = runner.invoke(evolution_app, ["status"])
    assert result.exit_code == 0
    assert "85" in result.output
    assert "Decisions observed: 42" in result.output


def test_status_window_flag_emits_hint(_isolate_profile):
    """The --window flag triggers an aggregate hint about in-memory state."""
    result = runner.invoke(evolution_app, ["status", "--window"])
    assert result.exit_code == 0
    assert "rolling window" in result.output


# ─── tune ────────────────────────────────────────────────────────────


def test_tune_creates_state_file(_isolate_profile):
    """``oc evolution tune`` writes the JSON state file (idempotent)."""
    tuning_file = _isolate_profile / "skills" / "evolution_tuning.json"
    assert not tuning_file.exists()
    result = runner.invoke(evolution_app, ["tune"])
    assert result.exit_code == 0, result.output
    assert tuning_file.exists()
    # File contains schema version + defaults (empty in-memory window).
    data = json.loads(tuning_file.read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["confidence_threshold"] == DEFAULT_TUNING.confidence_threshold


def test_tune_displays_table(_isolate_profile):
    """Manual tune renders the tuning table with the recomputed state."""
    result = runner.invoke(evolution_app, ["tune"])
    assert result.exit_code == 0
    assert "recomputed" in result.output
    assert "confidence_threshold" in result.output


# ─── reset ───────────────────────────────────────────────────────────


def test_reset_without_yes_prompts(_isolate_profile):
    """Reset without --yes asks for confirmation (denied by default → cancels)."""
    # CliRunner provides no stdin input → typer.confirm gets EOF → False.
    result = runner.invoke(evolution_app, ["reset"], input="\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


def test_reset_with_yes_restores_defaults(_isolate_profile):
    """--yes skips the prompt and writes default tuning."""
    # Pre-populate with non-default tuning.
    (_isolate_profile / "skills" / "evolution_tuning.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "confidence_threshold": 90,
                "dreaming_v2_score_threshold": 0.85,
                "dreaming_v2_min_recall": 4,
                "decisions_observed": 50,
                "last_recompute_ts": 1700000000.0,
            }
        )
    )
    assert load_tuning(_isolate_profile).confidence_threshold == 90

    result = runner.invoke(evolution_app, ["reset", "--yes"])
    assert result.exit_code == 0
    assert "reset to defaults" in result.output

    final = load_tuning(_isolate_profile)
    assert final.confidence_threshold == DEFAULT_TUNING.confidence_threshold
    assert final.dreaming_v2_score_threshold == DEFAULT_TUNING.dreaming_v2_score_threshold
    assert final.dreaming_v2_min_recall == DEFAULT_TUNING.dreaming_v2_min_recall


def test_reset_short_flag_works(_isolate_profile):
    """The ``-y`` short flag is equivalent to ``--yes``."""
    result = runner.invoke(evolution_app, ["reset", "-y"])
    assert result.exit_code == 0
    assert "reset to defaults" in result.output
