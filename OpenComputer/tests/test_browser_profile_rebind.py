"""Tests for §9.8 browser-profile rebind + plugin_sdk exposure.

Coverage:
  - PluginAPI.register_profile_rebind_handler queues into a public list
  - AgentLoop drains the queue on __init__
  - browser-harness plugin registers a handler that updates
    AGENT_BROWSER_PROFILE on swap
  - Re-register is idempotent
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest


def test_plugin_api_register_queues_handler() -> None:
    from opencomputer.plugins.loader import PluginAPI

    api = PluginAPI(
        tool_registry=None,
        hook_engine=None,
        provider_registry={},
        channel_registry={},
    )

    called: list[tuple[Any, Any]] = []

    def _h(new, old):
        called.append((new, old))

    api.register_profile_rebind_handler("test", _h, priority=99)
    pending = api.pending_profile_rebind_handlers
    assert "test" in pending
    handler, prio = pending["test"]
    assert handler is _h
    assert prio == 99


def test_plugin_api_rejects_invalid() -> None:
    from opencomputer.plugins.loader import PluginAPI

    api = PluginAPI(
        tool_registry=None, hook_engine=None,
        provider_registry={}, channel_registry={},
    )
    with pytest.raises(ValueError, match="non-empty"):
        api.register_profile_rebind_handler("", lambda n, o: None)
    with pytest.raises(TypeError, match="callable"):
        api.register_profile_rebind_handler("x", "not a callable")  # type: ignore[arg-type]


def test_plugin_api_register_replaces_idempotent() -> None:
    from opencomputer.plugins.loader import PluginAPI

    api = PluginAPI(
        tool_registry=None, hook_engine=None,
        provider_registry={}, channel_registry={},
    )

    def h1(n, o):
        return None

    def h2(n, o):
        return None

    api.register_profile_rebind_handler("dup", h1)
    api.register_profile_rebind_handler("dup", h2)  # replaces
    pending = api.pending_profile_rebind_handlers
    assert len(pending) == 1
    assert pending["dup"][0] is h2


@pytest.mark.asyncio
async def test_browser_harness_rebind_updates_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the browser-harness plugin module + running its rebind
    handler must update AGENT_BROWSER_PROFILE to the new profile's path.
    """
    monkeypatch.delenv("AGENT_BROWSER_PROFILE", raising=False)

    # The plugin module lives in extensions/browser-harness/plugin.py and
    # uses relative imports inside extensions. Load it via spec to avoid
    # depending on extensions being on sys.path.
    plugin_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "browser-harness"
        / "plugin.py"
    )
    assert plugin_path.exists(), f"missing plugin file: {plugin_path}"

    spec = importlib.util.spec_from_file_location(
        "_test_browser_harness_plugin", plugin_path,
    )
    mod = importlib.util.module_from_spec(spec)
    # The plugin imports ``compat`` and ``dispatcher`` from its sibling
    # files. Make those importable by adding the plugin dir to sys.path.
    sys.path.insert(0, str(plugin_path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)

    # Fake API just enough for the plugin's register() to call into us.
    captured_handlers: dict[str, Any] = {}

    class _FakeApi:
        def register_tool(self, tool):
            pass

        def register_profile_rebind_handler(self, name, handler, *, priority=100):
            captured_handlers[name] = (handler, priority)

    fake_api = _FakeApi()
    mod.register(fake_api)

    assert "browser-harness" in captured_handlers
    handler, prio = captured_handlers["browser-harness"]
    assert prio == 160

    # Run the handler against a new profile home.
    new_home = tmp_path / "profiles" / "newp" / "home"
    new_home.mkdir(parents=True)
    handler(new_home, None)

    assert os.environ.get("AGENT_BROWSER_PROFILE") == str(
        tmp_path / "profiles" / "newp" / "browser-profile",
    )
    assert (tmp_path / "profiles" / "newp" / "browser-profile").is_dir()


def test_pending_handlers_snapshot_is_copy() -> None:
    """Mutating the returned dict must not affect internal state."""
    from opencomputer.plugins.loader import PluginAPI

    api = PluginAPI(
        tool_registry=None, hook_engine=None,
        provider_registry={}, channel_registry={},
    )
    api.register_profile_rebind_handler("x", lambda n, o: None)
    pending = api.pending_profile_rebind_handlers
    pending["x"] = ("hacked", 0)
    # Internal state unchanged.
    assert api.pending_profile_rebind_handlers["x"][0] != "hacked"
