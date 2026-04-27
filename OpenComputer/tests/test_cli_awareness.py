"""V2.C-T3 — CLI tests for ``opencomputer awareness patterns ...``.

Personas tests are deferred to T4 (the registry doesn't exist yet); the
``personas list`` command is exercised here only to verify the graceful
ImportError fallback so the CLI surface stays usable mid-task.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()


def test_patterns_list_shows_six_default(tmp_path, monkeypatch):
    """`awareness patterns list` enumerates all six T1+T2 patterns."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "patterns", "list"])
    assert result.exit_code == 0, result.stdout
    # All six default patterns should appear in the table.
    for pattern_id in (
        "job_change",
        "exam_prep",
        "burnout",
        "relationship_shift",
        "health_event",
        "travel",
    ):
        assert pattern_id in result.stdout, (
            f"Pattern {pattern_id!r} missing from output:\n{result.stdout}"
        )
    # Header row is present.
    assert "pattern_id" in result.stdout
    assert "surfacing" in result.stdout
    assert "muted" in result.stdout


def test_patterns_mute_persists(tmp_path, monkeypatch):
    """Muting writes the pattern ID to the persistent JSON list."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "patterns", "mute", "burnout"])
    assert result.exit_code == 0, result.stdout
    assert "Muted: burnout" in result.stdout

    state_path = tmp_path / "awareness" / "muted_patterns.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state == ["burnout"]

    # `list` should now reflect the muted state.
    list_result = runner.invoke(app, ["awareness", "patterns", "list"])
    assert list_result.exit_code == 0
    # Find the burnout row and confirm it shows "yes" for muted.
    burnout_lines = [
        ln for ln in list_result.stdout.splitlines() if ln.startswith("burnout")
    ]
    assert len(burnout_lines) == 1
    assert "yes" in burnout_lines[0]


def test_patterns_mute_is_idempotent(tmp_path, monkeypatch):
    """Muting an already-muted pattern doesn't duplicate the entry."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["awareness", "patterns", "mute", "burnout"])
    runner.invoke(app, ["awareness", "patterns", "mute", "burnout"])

    state_path = tmp_path / "awareness" / "muted_patterns.json"
    state = json.loads(state_path.read_text())
    assert state == ["burnout"]


def test_patterns_unmute_removes_entry(tmp_path, monkeypatch):
    """Unmuting strips the pattern ID and `list` reflects it."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["awareness", "patterns", "mute", "burnout"])
    result = runner.invoke(app, ["awareness", "patterns", "unmute", "burnout"])
    assert result.exit_code == 0, result.stdout
    assert "Unmuted: burnout" in result.stdout

    state_path = tmp_path / "awareness" / "muted_patterns.json"
    state = json.loads(state_path.read_text())
    assert "burnout" not in state


def test_patterns_unmute_no_state_file_is_quiet(tmp_path, monkeypatch):
    """Calling unmute with no prior mute state is a no-op, not an error."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "patterns", "unmute", "burnout"])
    assert result.exit_code == 0, result.stdout
    assert "Nothing muted" in result.stdout


def test_unknown_pattern_id_errors(tmp_path, monkeypatch):
    """`mute <unknown>` exits non-zero and lists known patterns on stderr."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(
        app, ["awareness", "patterns", "mute", "not_a_real_pattern"]
    )
    assert result.exit_code == 1
    # Click/Typer routes err=True writes to stderr; CliRunner merges by default.
    combined = (result.stdout or "") + (getattr(result, "stderr", "") or "")
    assert "Unknown pattern" in combined
    assert "not_a_real_pattern" in combined


def test_personas_list_graceful_when_registry_missing(tmp_path, monkeypatch):
    """V2.C-T4 hasn't shipped the registry; `personas list` must not crash."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "personas", "list"])
    assert result.exit_code == 0, result.stdout
    # Either the stub message (T4 pending) or an actual persona table — both
    # are acceptable. We only require that it exits 0 and prints something.
    assert result.stdout.strip() != ""


def test_capability_taxonomy_includes_awareness_claims():
    """V2.C-T3 — four awareness.* claims registered as IMPLICIT."""
    from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
    from plugin_sdk import ConsentTier

    assert F1_CAPABILITIES["awareness.life_event.observe"] is ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["awareness.life_event.surface"] is ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["awareness.persona.classify"] is ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["awareness.persona.switch"] is ConsentTier.IMPLICIT
