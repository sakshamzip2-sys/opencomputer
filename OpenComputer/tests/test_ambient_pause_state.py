"""tests/test_ambient_pause_state.py"""
from __future__ import annotations

import dataclasses
import json
import time

import pytest
from extensions.ambient_sensors.pause_state import (
    AmbientState,
    is_currently_paused,
    load_state,
    save_state,
)


def test_load_missing_returns_default(tmp_path):
    """Missing state.json → default (disabled). This is the privacy-safe default."""
    state = load_state(tmp_path / "state.json")
    assert state.enabled is False
    assert state.paused_until is None


def test_load_corrupt_json_returns_default(tmp_path):
    """Corrupt JSON → default (disabled), not raise."""
    p = tmp_path / "state.json"
    p.write_text("{ this is not json")
    state = load_state(p)
    assert state.enabled is False


def test_save_then_load_round_trip(tmp_path):
    p = tmp_path / "state.json"
    save_state(p, AmbientState(enabled=True, paused_until=None, sensors=("foreground",)))
    loaded = load_state(p)
    assert loaded.enabled is True
    assert loaded.paused_until is None
    assert loaded.sensors == ("foreground",)


def test_save_creates_parent_dir(tmp_path):
    """save_state must create the ambient/ subdir if missing."""
    p = tmp_path / "ambient" / "state.json"
    save_state(p, AmbientState(enabled=True, sensors=("foreground",)))
    assert p.exists()


def test_save_paused_until_persists(tmp_path):
    p = tmp_path / "state.json"
    until = time.time() + 3600
    save_state(p, AmbientState(enabled=True, paused_until=until, sensors=("foreground",)))
    loaded = load_state(p)
    assert loaded.paused_until == pytest.approx(until)


def test_pause_until_in_future_means_paused():
    state = AmbientState(enabled=True, paused_until=time.time() + 60, sensors=("foreground",))
    assert is_currently_paused(state) is True


def test_pause_until_in_past_means_not_paused():
    state = AmbientState(enabled=True, paused_until=time.time() - 60, sensors=("foreground",))
    assert is_currently_paused(state) is False


def test_no_pause_until_means_not_paused():
    state = AmbientState(enabled=True, paused_until=None, sensors=("foreground",))
    assert is_currently_paused(state) is False


def test_disabled_state_means_not_paused():
    """Disabled is a stronger state than paused — pause check returns False
    regardless of paused_until. (Daemon also won't run when disabled.)"""
    state = AmbientState(enabled=False, paused_until=time.time() + 60, sensors=("foreground",))
    assert is_currently_paused(state) is False


def test_state_is_frozen():
    state = AmbientState(enabled=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.enabled = False


def test_save_writes_pretty_json_for_humans(tmp_path):
    """The state file is human-editable; output should be readable JSON with indentation."""
    p = tmp_path / "state.json"
    save_state(p, AmbientState(enabled=True, paused_until=None, sensors=("foreground",)))
    content = p.read_text()
    # Indented JSON has newlines + spaces; minified would be on one line
    assert "\n" in content
    assert json.loads(content) == {
        "enabled": True,
        "paused_until": None,
        "sensors": ["foreground"],
    }
