"""Layer D — manifest ``enabled_by_default`` auto-include.

When a plugin's ``plugin.json`` declares ``enabled_by_default: true`` it
must load even if the user's ``profile.yaml`` carries a narrow explicit
``plugins.enabled: [...]`` list that omits it. Layer D mirrors Layer C
(model-prefix auto-activation) and runs BEFORE Layer B's filter.

Without this guarantee, plugins shipped as "core" features (browser
automation, default memory, etc.) would silently disappear the moment a
user pinned a profile to a small set — which is the trap that hid
browser-control's real bug for several days.

Regression guard: also pins the bundled browser-control manifest's
``enabled_by_default`` flag so a future edit can't quietly demote it.
"""

from __future__ import annotations

import json
from pathlib import Path

from opencomputer.plugins import discovery
from opencomputer.plugins.registry import PluginRegistry


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    enabled_by_default: bool = False,
    profiles: list[str] | None = None,
) -> None:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "tool",
        "entry": "plugin",
        "enabled_by_default": enabled_by_default,
    }
    if profiles is not None:
        manifest["profiles"] = profiles
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")


def _clear_caches() -> None:
    discovery._discovery_cache.clear()


def test_layer_d_force_includes_core_plugin(tmp_path: Path) -> None:
    """A plugin with ``enabled_by_default: true`` loads even when the
    user's ``enabled_ids`` narrowly excludes it."""
    _clear_caches()
    plugin_root = tmp_path / "plugins"
    _write_plugin(plugin_root, "core-tool", enabled_by_default=True)
    _write_plugin(plugin_root, "optional-tool", enabled_by_default=False)
    _write_plugin(plugin_root, "user-explicit", enabled_by_default=False)

    registry = PluginRegistry()
    # User pinned only ``user-explicit``. ``core-tool`` should still load
    # via Layer D; ``optional-tool`` must stay filtered out.
    registry.load_all([plugin_root], enabled_ids=frozenset({"user-explicit"}))

    loaded = {lp.candidate.manifest.id for lp in registry.loaded}
    assert "core-tool" in loaded, (
        f"enabled_by_default=true must force-include the plugin even with a narrow "
        f"enabled_ids list. loaded={loaded}"
    )
    assert "user-explicit" in loaded
    assert "optional-tool" not in loaded


def test_layer_d_skipped_when_wildcard(tmp_path: Path) -> None:
    """Wildcard mode (``enabled_ids=None``) already loads everything;
    Layer D must not double-process or fight it."""
    _clear_caches()
    plugin_root = tmp_path / "plugins"
    _write_plugin(plugin_root, "core-tool", enabled_by_default=True)
    _write_plugin(plugin_root, "optional-tool", enabled_by_default=False)

    registry = PluginRegistry()
    registry.load_all([plugin_root], enabled_ids=None)  # wildcard

    loaded = {lp.candidate.manifest.id for lp in registry.loaded}
    # Both load — wildcard means everything.
    assert {"core-tool", "optional-tool"}.issubset(loaded)


def test_layer_d_respects_layer_a_profile_scope(tmp_path: Path) -> None:
    """A core plugin that scopes itself via ``profiles: [coding]`` must
    NOT auto-load in the ``default`` profile. Layer A is the AUTHOR's
    compatibility statement; Layer D never overrides it.

    The active profile here is ``default`` (no OPENCOMPUTER_PROFILE set
    in the test env), so a plugin with ``profiles: ["coding"]`` must be
    skipped by Layer A and Layer D must not re-add it.
    """
    _clear_caches()
    plugin_root = tmp_path / "plugins"
    _write_plugin(
        plugin_root,
        "coding-only-core",
        enabled_by_default=True,
        profiles=["coding"],
    )
    _write_plugin(
        plugin_root,
        "everywhere-core",
        enabled_by_default=True,
        profiles=["*"],
    )

    registry = PluginRegistry()
    registry.load_all([plugin_root], enabled_ids=frozenset())

    loaded = {lp.candidate.manifest.id for lp in registry.loaded}
    assert "everywhere-core" in loaded
    assert "coding-only-core" not in loaded, (
        "Layer A (profile scope) must veto Layer D's auto-include — a coding-only "
        "plugin should not auto-load in the default profile"
    )


def test_browser_control_manifest_pins_enabled_by_default() -> None:
    """Regression guard: the bundled browser-control manifest must keep
    ``enabled_by_default: true`` so the agent always has a JS-capable
    browser. SPA / client-rendered pages fail with WebFetch alone; this
    flag is the only thing that survives a user pinning their profile to
    a narrow ``enabled: [...]`` list.
    """
    path = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "browser-control"
        / "plugin.json"
    )
    data = json.loads(path.read_text())
    assert data.get("enabled_by_default") is True, (
        "browser-control must remain enabled_by_default=true. Without it, the "
        "plugin disappears from any profile.yaml that lists explicit enabled IDs, "
        "leaving the agent with no JS-capable browser."
    )
