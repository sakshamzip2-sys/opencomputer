"""tests/test_skill_evolution_cli.py — T6 CLI tests for `oc skills`.

Hard privacy contracts:
- ``skills evolution status`` output is aggregate-only (counts + timings).
  It must never leak specific session IDs, app names, or skill content.
- ``skills list`` may show proposed-skill NAMES + DESCRIPTIONS but not
  provenance.session_id.
- ``skills review`` (interactive) may show the full proposal body
  because the user themselves drove the request.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from opencomputer.cli_skills import app


def _create_proposal(profile_home, name="auto-test", description="Use when testing"):
    pdir = profile_home / "skills" / "_proposed" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# Test\n\nbody"
    )
    (pdir / "provenance.json").write_text(
        json.dumps(
            {
                "session_id": "sess123",
                "generated_at": 1717800000.0,
                "confidence_score": 80,
                "source_summary": "test",
                "description": description,
            }
        )
    )


def test_evolution_on_writes_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["evolution", "on"])
    assert result.exit_code == 0, result.output
    state = json.loads((tmp_path / "skills" / "evolution_state.json").read_text())
    assert state["enabled"] is True


def test_evolution_off_writes_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["evolution", "on"])
    result = CliRunner().invoke(app, ["evolution", "off"])
    assert result.exit_code == 0, result.output
    state = json.loads((tmp_path / "skills" / "evolution_state.json").read_text())
    assert state["enabled"] is False


def test_evolution_status_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["evolution", "status"])
    assert result.exit_code == 0
    assert "disabled" in result.output.lower() or "not enabled" in result.output.lower()


def test_evolution_status_does_not_leak_names(tmp_path, monkeypatch):
    """Status output must NOT contain specific session IDs / skill names."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _create_proposal(tmp_path, name="auto-hyper-secret-pattern")
    CliRunner().invoke(app, ["evolution", "on"])
    result = CliRunner().invoke(app, ["evolution", "status"])
    assert result.exit_code == 0
    assert "auto-hyper-secret-pattern" not in result.output
    assert "sess123" not in result.output


def test_list_shows_active_and_proposed(tmp_path, monkeypatch):
    """List output marks proposed clearly."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    # Add an active skill
    active = tmp_path / "skills" / "active-skill"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text(
        "---\nname: active-skill\ndescription: Active one\n---\n\nbody"
    )
    # Add a proposed
    _create_proposal(tmp_path, name="auto-proposed", description="Proposed one")

    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0, result.output
    assert "active-skill" in result.output
    assert "auto-proposed" in result.output
    # Proposed should be visually distinct
    assert "proposed" in result.output.lower()


def test_accept_moves_proposal_to_active(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _create_proposal(tmp_path, name="auto-accept-me")
    result = CliRunner().invoke(app, ["accept", "auto-accept-me"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "skills" / "auto-accept-me" / "SKILL.md").exists()
    assert not (tmp_path / "skills" / "_proposed" / "auto-accept-me").exists()


def test_reject_deletes_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _create_proposal(tmp_path, name="auto-reject-me")
    result = CliRunner().invoke(app, ["reject", "auto-reject-me"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "skills" / "_proposed" / "auto-reject-me").exists()


def test_accept_missing_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["accept", "auto-nonexistent"])
    assert result.exit_code != 0


def test_reject_missing_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["reject", "auto-nonexistent"])
    assert result.exit_code != 0


def test_evolution_status_shows_aggregate_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    CliRunner().invoke(app, ["evolution", "on"])
    _create_proposal(tmp_path, name="auto-1")
    _create_proposal(tmp_path, name="auto-2")
    _create_proposal(tmp_path, name="auto-3")

    result = CliRunner().invoke(app, ["evolution", "status"])
    assert result.exit_code == 0
    assert "3" in result.output  # count appears
    # But no specific names
    assert "auto-1" not in result.output
    assert "auto-2" not in result.output
    assert "auto-3" not in result.output
    assert "sess123" not in result.output


def test_review_skip_action(tmp_path, monkeypatch):
    """User skips a proposal — it stays in _proposed/."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _create_proposal(tmp_path, name="auto-skip-me")
    # Type "s" then "q" to skip then quit
    result = CliRunner().invoke(app, ["review"], input="s\nq\n")
    assert (tmp_path / "skills" / "_proposed" / "auto-skip-me").exists()


def test_review_accept_action(tmp_path, monkeypatch):
    """`a` in review accepts the proposal."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _create_proposal(tmp_path, name="auto-review-accept")
    result = CliRunner().invoke(app, ["review"], input="a\nq\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "skills" / "auto-review-accept" / "SKILL.md").exists()
    assert not (tmp_path / "skills" / "_proposed" / "auto-review-accept").exists()


def test_review_reject_action(tmp_path, monkeypatch):
    """`r` in review deletes the proposal."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    _create_proposal(tmp_path, name="auto-review-reject")
    result = CliRunner().invoke(app, ["review"], input="r\nq\n")
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "skills" / "_proposed" / "auto-review-reject").exists()


def test_review_no_proposals(tmp_path, monkeypatch):
    """Review with no proposals exits cleanly."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["review"])
    assert result.exit_code == 0
    assert "no" in result.output.lower() or "empty" in result.output.lower()
