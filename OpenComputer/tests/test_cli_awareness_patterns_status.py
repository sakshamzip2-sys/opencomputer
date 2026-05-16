"""Task 8 — CLI tests for ``oc awareness patterns status``.

``status`` renders the active life-event "teeth" recorded in
``life_event_state.json``: one Rich-table row per pattern with a pending
or scheduled follow-up. ``OPENCOMPUTER_HOME`` is monkey-patched to a
``tmp_path`` so the real user profile is never read or written — the
``state`` module resolves the file under ``OPENCOMPUTER_HOME``.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.awareness.life_events import state
from opencomputer.cli import app

runner = CliRunner()


def test_status_shows_seeded_pattern(tmp_path, monkeypatch):
    """A seeded ``life_event_state.json`` surfaces in the status table."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # mark_surfaced writes a full entry: firing_ts/cron_id/surfaced/
    # verdict_pending/surfaced_turn.
    state.mark_surfaced("exam_prep", "cron-99", surfaced_turn=4)

    result = runner.invoke(app, ["awareness", "patterns", "status"])
    assert result.exit_code == 0, result.stdout
    # The pattern id, its follow-up cron id, and the verdict-pending
    # column must all appear.
    assert "exam_prep" in result.stdout
    assert "cron-99" in result.stdout
    assert "verdict" in result.stdout.lower()
    # A verdict-pending entry renders a truthy marker.
    assert "yes" in result.stdout.lower()


def test_status_empty_state_friendly_message(tmp_path, monkeypatch):
    """No state file → a friendly one-liner, exit 0, no crash, no table."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["awareness", "patterns", "status"])
    assert result.exit_code == 0, result.stdout
    assert "No active life-event check-ins" in result.stdout


def test_status_tolerates_corrupt_non_dict_entry(tmp_path, monkeypatch):
    """A non-dict entry value in the state renders a dash-row instead of crashing."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    state.save_state({"exam_prep": "not-a-dict"})

    result = runner.invoke(app, ["awareness", "patterns", "status"])
    assert result.exit_code == 0, result.stdout
    assert "exam_prep" in result.stdout
