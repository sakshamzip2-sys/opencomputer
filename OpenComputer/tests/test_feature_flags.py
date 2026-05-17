"""P2-2: persistent feature_flags.json + kill switch substrate."""
from __future__ import annotations

import json

from opencomputer.agent.feature_flags import (
    DEFAULT_LIFE_EVENTS,
    DEFAULT_POLICY_FLAGS,
    FeatureFlags,
)


def test_defaults_when_file_missing(tmp_path):
    f = FeatureFlags(tmp_path / "feature_flags.json")
    assert f.read("policy_engine.enabled", default=True) is True
    assert f.read("policy_engine.daily_change_budget", default=3) == 3


def test_defaults_match_spec(tmp_path):
    f = FeatureFlags(tmp_path / "feature_flags.json")
    assert f.read("policy_engine.enabled") is True
    assert f.read("policy_engine.auto_approve_after_n_safe_decisions") == 10
    assert f.read("policy_engine.daily_change_budget") == 3
    assert f.read("policy_engine.min_eligible_turns_for_revert") == 10
    assert f.read("policy_engine.revert_threshold_sigma") == 1.0
    assert f.read("policy_engine.decay_factor_per_day") == 0.95
    assert f.read("policy_engine.minimum_deviation_threshold") == 0.10


def test_write_then_read(tmp_path):
    f = FeatureFlags(tmp_path / "feature_flags.json")
    f.write("policy_engine.enabled", False)
    f.write("policy_engine.daily_change_budget", 5)
    assert f.read("policy_engine.enabled") is False
    assert f.read("policy_engine.daily_change_budget") == 5


def test_atomic_write_lands_on_disk(tmp_path):
    path = tmp_path / "feature_flags.json"
    f = FeatureFlags(path)
    f.write("policy_engine.enabled", False)
    data = json.loads(path.read_text())
    assert data["policy_engine"]["enabled"] is False


def test_default_set_returned_by_read_all(tmp_path):
    f = FeatureFlags(tmp_path / "feature_flags.json")
    flags = f.read_all()
    assert flags["policy_engine"] == DEFAULT_POLICY_FLAGS


def test_kill_switch_persistent_across_instances(tmp_path):
    path = tmp_path / "feature_flags.json"
    f1 = FeatureFlags(path)
    f1.write("policy_engine.enabled", False)
    del f1

    f2 = FeatureFlags(path)
    assert f2.read("policy_engine.enabled") is False


def test_unknown_key_returns_default(tmp_path):
    f = FeatureFlags(tmp_path / "feature_flags.json")
    assert f.read("policy_engine.totally_made_up", default=42) == 42
    assert f.read("not.a.real.path", default="x") == "x"


def test_corrupt_json_falls_back_to_defaults(tmp_path):
    path = tmp_path / "feature_flags.json"
    path.write_text("this is not json")
    f = FeatureFlags(path)
    assert f.read("policy_engine.enabled") is True  # default


# ---------------------------------------------------------------------------
# life_events.* namespace
# ---------------------------------------------------------------------------


def test_life_events_default_when_file_missing(tmp_path):
    """Absent flag file → multi_surface_life_events returns False."""
    f = FeatureFlags(tmp_path / "feature_flags.json")
    assert f.read("life_events.multi_surface_life_events") is False


def test_life_events_write_then_read(tmp_path):
    """Written True value round-trips through write() / read()."""
    f = FeatureFlags(tmp_path / "feature_flags.json")
    f.write("life_events.multi_surface_life_events", True)
    assert f.read("life_events.multi_surface_life_events") is True


def test_life_events_unknown_key_returns_caller_default(tmp_path):
    """Unknown life_events.<something> returns the caller-supplied default."""
    f = FeatureFlags(tmp_path / "feature_flags.json")
    assert f.read("life_events.no_such_flag", default="sentinel") == "sentinel"
    assert f.read("life_events.no_such_flag") is None


def test_life_events_not_seeded_into_read_all(tmp_path):
    """life_events is NOT pre-seeded by read_all() (only policy_engine is)."""
    f = FeatureFlags(tmp_path / "feature_flags.json")
    flags = f.read_all()
    assert "life_events" not in flags


def test_life_events_default_constant_shape():
    """DEFAULT_LIFE_EVENTS exists and has the expected key with False default."""
    assert "multi_surface_life_events" in DEFAULT_LIFE_EVENTS
    assert DEFAULT_LIFE_EVENTS["multi_surface_life_events"] is False
