"""Smoke test: every new plugin in this PR loads cleanly through the OC loader.

Catches "manifest passes JSON-Schema but ``register(api)`` blows up at
import time" — the failure mode my unit tests can't see because they
load the plugin's modules directly via ``importlib.util``, bypassing
the real loader's synthetic-module-name + sys-path-injection setup.

Run path: ``discover([extensions/])`` → filter to the 4 plugins
introduced in this PR → for each, build a stub ``PluginAPI`` and call
``load_plugin``. Pass = no exceptions and the expected tools / hooks
showed up in the stub registries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencomputer.plugins.discovery import discover
from opencomputer.plugins.loader import PluginAPI, load_plugin

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXTENSIONS_DIR = REPO_ROOT / "extensions"

# Plugins this PR introduces. The smoke test asserts each loads cleanly
# and registers what its plugin.json claims.
NEW_PLUGINS: dict[str, dict[str, Any]] = {
    "code-modernization": {
        "expect_tools": (),  # no Python tools registered; skills + agents only
        "expect_hooks": False,
    },
    "lsp-bridge": {
        "expect_tools": ("LspDiagnostics",),
        "expect_hooks": False,
    },
    "security-guidance": {
        "expect_tools": (),  # registers a hook, not a tool
        "expect_hooks": True,
    },
    "hookify": {
        "expect_tools": (),
        "expect_hooks": True,
    },
}


class _StubToolRegistry:
    """Minimum surface ``register_tool`` + the loader's contract validator need.

    The loader reads ``api.tools.names()`` to snapshot which tools are
    registered (used for the runtime contract warning when a plugin
    declares ``tool_names`` in its manifest but registers nothing). We
    expose ``names()`` so the contract check matches reality.
    """

    def __init__(self) -> None:
        self.registered: list[Any] = []

    def register(self, tool: Any) -> None:
        self.registered.append(tool)

    register_tool = register

    def names(self) -> list[str]:
        return [t.schema.name for t in self.registered]


class _StubHookEngine:
    """Minimum surface ``register_hook`` + the loader's contract validator need.

    The loader reads ``api.hooks._hooks`` (a dict keyed by event) when
    snapshotting hook registrations for the contract diff. We expose
    that shape so the snapshot matches reality.
    """

    def __init__(self) -> None:
        self.specs: list[Any] = []
        self._hooks: dict[Any, list[Any]] = {}

    def register(self, spec: Any) -> None:
        self.specs.append(spec)
        self._hooks.setdefault(spec.event, []).append(spec)

    register_hook = register


def _make_api() -> PluginAPI:
    return PluginAPI(
        tool_registry=_StubToolRegistry(),
        hook_engine=_StubHookEngine(),
        provider_registry={},
        channel_registry={},
    )


def _candidate_for(plugin_id: str):
    """Return the discovered candidate for ``plugin_id`` from extensions/."""
    candidates = discover([EXTENSIONS_DIR], force_rescan=True)
    for c in candidates:
        if c.manifest.id == plugin_id:
            return c
    raise AssertionError(
        f"plugin {plugin_id!r} not found by discover([{EXTENSIONS_DIR}]) — "
        f"discovered {[c.manifest.id for c in candidates]!r}"
    )


@pytest.mark.parametrize("plugin_id", sorted(NEW_PLUGINS))
def test_new_plugin_loads_without_exception(plugin_id: str) -> None:
    """Smoke: discover + load + register(api) succeeds for the plugin."""
    candidate = _candidate_for(plugin_id)
    api = _make_api()
    loaded = load_plugin(candidate, api)
    assert loaded is not None, (
        f"{plugin_id} returned None from load_plugin — see logs for "
        "PluginIncompatibleError / PluginCLINameCollision / activation lock"
    )
    assert loaded.candidate.manifest.id == plugin_id


@pytest.mark.parametrize("plugin_id", sorted(NEW_PLUGINS))
def test_new_plugin_registers_expected_surface(plugin_id: str) -> None:
    """For each plugin, verify it registered what its manifest advertises."""
    spec = NEW_PLUGINS[plugin_id]
    candidate = _candidate_for(plugin_id)
    api = _make_api()
    load_plugin(candidate, api)

    registered_tool_names = {
        t.schema.name
        for t in api.tools.registered  # type: ignore[attr-defined]
    }
    for expected in spec["expect_tools"]:
        assert expected in registered_tool_names, (
            f"{plugin_id} did not register expected tool {expected!r}; "
            f"got {sorted(registered_tool_names)!r}"
        )

    if spec["expect_hooks"]:
        assert (
            len(api.hooks.specs) > 0  # type: ignore[attr-defined]
        ), f"{plugin_id} registered zero hooks but its manifest implies it should"
    else:
        # Plugins that aren't hook-shaped MAY still register hooks (e.g.
        # lsp-bridge could grow one) — only assert when the manifest
        # explicitly says no hooks. Skip the negative assertion.
        pass


def test_all_new_plugins_discovered_at_least_once() -> None:
    """Failsafe: extensions/ should surface every plugin in NEW_PLUGINS."""
    candidates = discover([EXTENSIONS_DIR], force_rescan=True)
    discovered_ids = {c.manifest.id for c in candidates}
    missing = set(NEW_PLUGINS) - discovered_ids
    assert not missing, (
        f"extensions/ did not surface {missing!r}; check plugin.json validity "
        f"on disk (got {sorted(discovered_ids)!r})"
    )
