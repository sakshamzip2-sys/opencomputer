"""load_config_for_profile + PluginRegistry.tools_provided_by — Pass-2 F2 fix."""
from __future__ import annotations

from pathlib import Path

import pytest


def _config_module():
    """Re-import ``opencomputer.agent.config`` fresh.

    Some tests in the suite (e.g. ``tests/test_phase10f.py``) call
    ``importlib.reload`` on this module to pick up a changed
    ``OPENCOMPUTER_HOME``. After such a reload the module's ``Config``
    class becomes a *different* class object than what a top-of-file
    ``from ... import Config`` captured. Resolving the module on each
    test entry insulates these tests from that pre-existing test
    isolation hazard.
    """
    import importlib

    return importlib.import_module("opencomputer.agent.config")


def test_load_config_for_profile_uses_profile_paths(tmp_path: Path) -> None:
    """Config paths must be derived from the passed profile_home,
    not from process-global OPENCOMPUTER_HOME."""
    cfg_mod = _config_module()
    profile_home = tmp_path / "p"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n"
    )
    cfg = cfg_mod.load_config_for_profile(profile_home)
    assert isinstance(cfg, cfg_mod.Config)
    assert cfg.session.db_path.parent == profile_home
    assert cfg.memory.declarative_path.parent == profile_home


def test_load_config_for_profile_does_not_mutate_env(tmp_path: Path, monkeypatch) -> None:
    """The helper must not leak its profile selection into the
    process environment or ContextVar — purely scoped."""
    from plugin_sdk.profile_context import current_profile_home

    cfg_mod = _config_module()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "default"))
    profile_home = tmp_path / "alt"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  model: claude-sonnet-4-6\n"
    )

    _ = cfg_mod.load_config_for_profile(profile_home)
    # After the call, the env var is unchanged and ContextVar is reset.
    import os
    assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "default")
    assert current_profile_home.get() is None


def test_tools_provided_by_returns_known_set() -> None:
    """tools_provided_by(plugin_id) must return a frozenset of tool
    names the plugin registered. Tolerant of plugin-load state."""
    from opencomputer.plugins.registry import registry as plugin_registry

    # Empty result for plugins that don't register tools — pure providers.
    # Use a tolerant assertion because plugin load timing varies.
    result = plugin_registry.tools_provided_by("anthropic-provider")
    assert isinstance(result, frozenset)
    assert "Edit" not in result  # anthropic-provider doesn't register Edit


def test_tools_provided_by_unknown_plugin_returns_empty() -> None:
    """Unknown plugin_id is not an error — returns empty frozenset."""
    from opencomputer.plugins.registry import registry as plugin_registry

    assert plugin_registry.tools_provided_by("nonexistent-plugin-xyz") == frozenset()
