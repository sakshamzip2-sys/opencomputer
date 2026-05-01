"""Plan 1 of 3 — Profile UI port. Tests the cycle helper + swap consumer.

The persona auto-classifier still runs during Plan 1 (deleted in Plan 2),
so we deliberately leave persona-related runtime state alone.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opencomputer.cli_ui._profile_swap import (
    consume_pending_profile_swap,
    cycle_profile,
)


def _runtime() -> SimpleNamespace:
    """Fake RuntimeContext sufficient for the helpers under test."""
    return SimpleNamespace(custom={})


def _seed_profiles(root: Path, names: list[str]) -> None:
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / "profiles" / n).mkdir()


def test_cycle_profile_with_two_named_profiles_plus_default(tmp_path, monkeypatch):
    """default + work + side → cycles default → side → work → default → side."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work", "side"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    assert cycle_profile(runtime) == "side"
    assert runtime.custom["pending_profile_id"] == "side"

    runtime.custom["active_profile_id"] = "side"
    runtime.custom.pop("pending_profile_id", None)
    assert cycle_profile(runtime) == "work"

    runtime.custom["active_profile_id"] = "work"
    runtime.custom.pop("pending_profile_id", None)
    assert cycle_profile(runtime) == "default"


def test_cycle_profile_default_only_returns_none(tmp_path, monkeypatch):
    """Only the implicit default exists → no other profiles to cycle to."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    assert cycle_profile(runtime) is None
    assert runtime.custom.get("profile_cycle_hint") == (
        "no other profiles — use /profile create"
    )
    assert "pending_profile_id" not in runtime.custom


def test_cycle_profile_unknown_current_starts_from_first(tmp_path, monkeypatch):
    """If active_profile_id is missing/garbage, cycle starts from sorted[0]."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["alpha", "beta"])
    runtime = _runtime()
    # No active_profile_id set.
    assert cycle_profile(runtime) == "alpha"


def test_cycle_profile_re_press_advances_pending(tmp_path, monkeypatch):
    """Pressing Ctrl+P twice without a turn boundary advances pending."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work", "side"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"

    cycle_profile(runtime)  # → side
    assert runtime.custom["pending_profile_id"] == "side"

    cycle_profile(runtime)  # → work
    assert runtime.custom["pending_profile_id"] == "work"
