"""Tests for §9.1 — closing the OPENCOMPUTER_HOME asymmetry documented
in ``docs/plans/profile-handoff-investigation.md`` §3.

Before this fix, ``consume_pending_profile_swap`` updated the sticky
file (``~/.opencomputer/active_profile``) and ``runtime.custom`` but
NOT ``os.environ['OPENCOMPUTER_HOME']``. Result: subprocesses spawned
post-swap saw the NEW profile (via ``scope_subprocess_env`` reading
the sticky file) while in-process Python code calling ``_home()`` saw
the OLD profile (via ``OPENCOMPUTER_HOME`` env var). This split-brain
caused plugins doing lazy path resolution to read from the wrong
profile.

These tests assert: post-swap, ``OPENCOMPUTER_HOME`` matches the new
profile, ``_home()`` returns the new path, and the three-state
(env, sticky, runtime) is consistent.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(custom={})


def _seed_profile_root(root: Path, name: str) -> Path:
    """Create the on-disk profile dir so write_active_profile works."""
    pdir = root / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def test_consume_pending_updates_opencomputer_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.1: env var must update atomically with sticky file."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profile_root(tmp_path, "stocks")
    # Simulate pre-swap state: env var points at default root
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.cli_ui._profile_swap import consume_pending_profile_swap

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "stocks"

    result = consume_pending_profile_swap(runtime)
    assert result == "stocks"

    import os
    # The env var MUST now point at the stocks profile root.
    assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "stocks")


def test_consume_pending_swap_to_default_clears_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap back to default → env var resets to default root."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profile_root(tmp_path, "work")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "profiles" / "work"))

    from opencomputer.cli_ui._profile_swap import consume_pending_profile_swap

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "work"
    runtime.custom["pending_profile_id"] = "default"

    consume_pending_profile_swap(runtime)

    import os
    # Default profile uses the root directly, not a /profiles/<name>/ subdir.
    assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path)


def test_home_resolver_returns_new_path_post_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_home() must return the new profile path immediately after consume."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profile_root(tmp_path, "alpha")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.agent.config import _home
    from opencomputer.cli_ui._profile_swap import consume_pending_profile_swap

    # Pre-swap _home() reads OPENCOMPUTER_HOME = tmp_path
    assert _home() == tmp_path

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "alpha"

    consume_pending_profile_swap(runtime)

    # Post-swap _home() must return alpha's home — this is the bug fix.
    assert _home() == tmp_path / "profiles" / "alpha"


def test_consume_pending_no_swap_does_not_mutate_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pending swap → env var must NOT be touched."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    original = str(tmp_path / "untouched")
    monkeypatch.setenv("OPENCOMPUTER_HOME", original)

    from opencomputer.cli_ui._profile_swap import consume_pending_profile_swap

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    # No pending_profile_id

    result = consume_pending_profile_swap(runtime)
    assert result is None

    import os
    assert os.environ["OPENCOMPUTER_HOME"] == original


def test_consume_pending_swap_to_same_profile_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap to the currently-active profile → no env change."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profile_root(tmp_path, "same")
    original = str(tmp_path / "profiles" / "same")
    monkeypatch.setenv("OPENCOMPUTER_HOME", original)

    from opencomputer.cli_ui._profile_swap import consume_pending_profile_swap

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "same"
    runtime.custom["pending_profile_id"] = "same"

    result = consume_pending_profile_swap(runtime)
    assert result is None

    import os
    assert os.environ["OPENCOMPUTER_HOME"] == original


def test_three_state_consistency_after_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-swap, env var + sticky file + runtime.custom must all agree."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profile_root(tmp_path, "agreed")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.cli_ui._profile_swap import consume_pending_profile_swap
    from opencomputer.profiles import read_active_profile

    runtime = _runtime()
    runtime.custom["active_profile_id"] = "default"
    runtime.custom["pending_profile_id"] = "agreed"

    consume_pending_profile_swap(runtime)

    import os
    # All three sources of truth must agree.
    assert runtime.custom["active_profile_id"] == "agreed"
    assert read_active_profile() == "agreed"
    assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "agreed")


def test_contextvar_resets_so_plugin_paths_see_new_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin_sdk.current_profile_home ContextVar must be cleared on swap.

    Plugins set this for per-task scoping; a stale value pinned to the
    old profile would silently route plugin path lookups (auth_discovery,
    skill paths) to the wrong profile.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    _seed_profile_root(tmp_path, "newp")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from plugin_sdk.profile_context import current_profile_home

    # Pre-swap: a previous task pinned ContextVar to the OLD profile home.
    old_home = tmp_path / "old_pinned"
    token = current_profile_home.set(old_home)
    try:
        from opencomputer.cli_ui._profile_swap import consume_pending_profile_swap

        runtime = _runtime()
        runtime.custom["active_profile_id"] = "default"
        runtime.custom["pending_profile_id"] = "newp"

        consume_pending_profile_swap(runtime)

        # ContextVar should be cleared OR retargeted at the new profile;
        # critically, it must NOT still point at old_pinned.
        cv = current_profile_home.get()
        assert cv != old_home, (
            "ContextVar still pinned to pre-swap profile — plugins doing "
            "lazy path resolution will read the wrong profile."
        )
    finally:
        current_profile_home.reset(token)
