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
    init_active_profile_id,
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


def test_consume_swap_no_pending_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    assert consume_pending_profile_swap(runtime) is None


def test_consume_swap_same_as_current_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "work"
    runtime.custom["pending_profile_id"] = "work"
    assert consume_pending_profile_swap(runtime) is None
    assert "pending_profile_id" not in runtime.custom


def test_consume_swap_writes_sticky_and_updates_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "work"

    result = consume_pending_profile_swap(runtime)

    assert result == "work"
    assert runtime.custom["active_profile_id"] == "work"
    assert "pending_profile_id" not in runtime.custom
    sticky = (tmp_path / "active_profile").read_text().strip()
    assert sticky == "work"


def test_consume_swap_to_default_clears_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "work"
    runtime.custom["pending_profile_id"] = "default"

    result = consume_pending_profile_swap(runtime)

    assert result == "default"
    assert not (tmp_path / "active_profile").exists()


def test_init_active_profile_id_reads_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "work"


def test_init_active_profile_id_default_when_no_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    runtime = _runtime()
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "default"


def test_init_active_profile_id_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profiles(tmp_path, ["work"])
    (tmp_path / "active_profile").write_text("work\n")
    runtime = _runtime()
    runtime.custom["active_profile_id"] = "side"  # already set; do not overwrite
    init_active_profile_id(runtime)
    assert runtime.custom["active_profile_id"] == "side"


def test_memory_manager_rebind_to_profile(tmp_path):
    """rebind_to_profile re-resolves the 3 path attributes to a new
    profile home so subsequent read_* calls hit the new files."""
    from opencomputer.agent.memory import MemoryManager

    profile_a = tmp_path / "a"
    profile_b = tmp_path / "b"
    (profile_a).mkdir()
    (profile_b).mkdir()
    (profile_a / "MEMORY.md").write_text("memory-A")
    (profile_a / "USER.md").write_text("user-A")
    (profile_a / "SOUL.md").write_text("soul-A")
    (profile_b / "MEMORY.md").write_text("memory-B")
    (profile_b / "USER.md").write_text("user-B")
    (profile_b / "SOUL.md").write_text("soul-B")

    skills = tmp_path / "skills"
    skills.mkdir()

    mm = MemoryManager(
        declarative_path=profile_a / "MEMORY.md",
        skills_path=skills,
        user_path=profile_a / "USER.md",
        soul_path=profile_a / "SOUL.md",
    )
    assert mm.read_declarative() == "memory-A"
    assert mm.read_user() == "user-A"
    assert mm.read_soul() == "soul-A"

    mm.rebind_to_profile(profile_b)

    assert mm.read_declarative() == "memory-B"
    assert mm.read_user() == "user-B"
    assert mm.read_soul() == "soul-B"
