"""tests/test_skill_evolution_doctor.py — T8 doctor preflight tests.

Validates :func:`opencomputer.doctor._check_skill_evolution_state`:

- Default off (state.json missing) → info-level pass.
- Explicitly disabled → info-level pass.
- Enabled with fresh heartbeat → pass.
- Enabled with stale heartbeat → warning.
- Enabled, no heartbeat at all → warning ("subscriber not running").
- Enabled with > 20 staged proposals → backlog warning telling the user
  to run ``opencomputer skills review``.

These mirror the ambient-doctor test shape so the two checks degrade
identically — neither blocks doctor exit when the feature is off.
"""

from __future__ import annotations

import json
import time

from opencomputer.doctor import _check_skill_evolution_state


def test_state_missing_returns_ok_disabled(tmp_path):
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is True
    assert "disabled" in result.message.lower()


def test_state_disabled_returns_ok(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "evolution_state.json").write_text(
        json.dumps({"enabled": False})
    )
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is True
    assert "disabled" in result.message.lower()


def test_enabled_with_fresh_heartbeat(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "evolution_state.json").write_text(
        json.dumps({"enabled": True})
    )
    (tmp_path / "skills" / "evolution_heartbeat").write_text(str(time.time()))
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is True
    assert "running" in result.message.lower()


def test_enabled_with_stale_heartbeat(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "evolution_state.json").write_text(
        json.dumps({"enabled": True})
    )
    (tmp_path / "skills" / "evolution_heartbeat").write_text(
        str(time.time() - 3600)
    )
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is False
    assert result.level == "warning"
    assert "stale" in result.message.lower()


def test_enabled_no_heartbeat(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "evolution_state.json").write_text(
        json.dumps({"enabled": True})
    )
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is False
    assert result.level == "warning"
    msg = result.message.lower()
    assert "heartbeat missing" in msg or "subscriber" in msg


def test_warns_on_too_many_proposals(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "evolution_state.json").write_text(
        json.dumps({"enabled": True})
    )
    (tmp_path / "skills" / "evolution_heartbeat").write_text(str(time.time()))
    proposed = tmp_path / "skills" / "_proposed"
    proposed.mkdir()
    for i in range(25):
        d = proposed / f"auto-{i}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: x\ndescription: y\n---\n\nbody"
        )
        (d / "provenance.json").write_text("{}")
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is False
    assert result.level == "warning"
    assert "25" in result.message
    assert "review" in result.message.lower()


def test_state_json_unreadable(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "evolution_state.json").write_text(
        "{not valid json"
    )
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is False
    assert result.level == "warning"
    assert "unreadable" in result.message.lower()


def test_proposal_count_under_threshold_is_ok(tmp_path):
    """Boundary check — 20 staged proposals is still OK; 21 trips the warning."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "evolution_state.json").write_text(
        json.dumps({"enabled": True})
    )
    (tmp_path / "skills" / "evolution_heartbeat").write_text(str(time.time()))
    proposed = tmp_path / "skills" / "_proposed"
    proposed.mkdir()
    for i in range(20):
        d = proposed / f"auto-{i}"
        d.mkdir()
    result = _check_skill_evolution_state(tmp_path)
    assert result.ok is True
    assert "20" in result.message
