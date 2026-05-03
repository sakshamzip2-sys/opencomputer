"""inspect_shape — compare manifest claims vs actual plugin registrations.

Sub-project G (openclaw-parity) Task 7. Pure helper
``inspect_shape_from_candidate`` is unit-tested with synthetic registry
data; the live ``inspect_shape(plugin_id)`` is exercised by Task 12's
end-to-end smoke test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.discovery import PluginCandidate
from opencomputer.plugins.inspect_shape import (
    PluginShape,
    inspect_shape,
    inspect_shape_from_candidate,
)
from plugin_sdk.core import PluginManifest


def _make_manifest(
    plugin_id: str,
    *,
    tool_names: tuple[str, ...] = (),
    optional_tool_names: tuple[str, ...] = (),
    kind: str = "tool",
    setup=None,
) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        description="",
        author="",
        homepage="",
        license="MIT",
        kind=kind,
        entry="plugin",
        profiles=None,
        single_instance=False,
        enabled_by_default=False,
        tool_names=tool_names,
        optional_tool_names=optional_tool_names,
        mcp_servers=(),
        model_support=None,
        legacy_plugin_ids=(),
        setup=setup,
        min_host_version="",
        activation=None,
    )


def _candidate(manifest: PluginManifest) -> PluginCandidate:
    return PluginCandidate(
        manifest=manifest,
        root_dir=Path("/tmp/fake"),
        manifest_path=Path("/tmp/fake/plugin.json"),
    )


class TestInspectShapeFromCandidate:
    def test_clean_plugin_classifies_valid(self) -> None:
        cand = _candidate(_make_manifest("clean", tool_names=("X",)))
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=("X",),
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "valid"
        assert shape.drift == ()
        assert shape.declared_tools == ("X",)
        assert shape.actual_tools == ("X",)

    def test_undeclared_tool_in_drift(self) -> None:
        cand = _candidate(_make_manifest("d", tool_names=("X",)))
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=("X", "Y"),
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("Y" in d for d in shape.drift)

    def test_declared_but_unregistered_tool_in_drift(self) -> None:
        cand = _candidate(_make_manifest("d", tool_names=("X", "Z")))
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=("X",),
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("Z" in d for d in shape.drift)

    def test_optional_tool_unregistered_is_not_drift(self) -> None:
        # Optional tool tolerated if absent at runtime (e.g. depends on
        # an extra pip extra not installed locally).
        cand = _candidate(
            _make_manifest("d", tool_names=("X",), optional_tool_names=("OptX",))
        )
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=("X",),
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "valid"

    def test_missing_required_tool_with_optional_present_still_drift(self) -> None:
        cand = _candidate(
            _make_manifest("d", tool_names=("X",), optional_tool_names=("OptX",))
        )
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=("OptX",),  # X missing, only OptX registered
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("X" in d and "declared but not registered" in d for d in shape.drift)


class TestChannelAndProviderDrift:
    """Symmetric drift detection for declared-vs-actual channels + providers."""

    def test_undeclared_channel_in_drift(self) -> None:
        from plugin_sdk.core import PluginSetup, SetupChannel

        cand = _candidate(
            _make_manifest(
                "ch",
                setup=PluginSetup(channels=(SetupChannel(id="telegram"),)),
            )
        )
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=(),
            registered_channels=("telegram", "discord"),  # discord not declared
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("discord" in d and "registered but not declared" in d for d in shape.drift)

    def test_declared_but_unregistered_channel_in_drift(self) -> None:
        from plugin_sdk.core import PluginSetup, SetupChannel

        cand = _candidate(
            _make_manifest(
                "ch",
                setup=PluginSetup(
                    channels=(
                        SetupChannel(id="telegram"),
                        SetupChannel(id="missing"),
                    )
                ),
            )
        )
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=(),
            registered_channels=("telegram",),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("missing" in d and "declared but not registered" in d for d in shape.drift)

    def test_undeclared_provider_in_drift(self) -> None:
        from plugin_sdk.core import PluginSetup, SetupProvider

        cand = _candidate(
            _make_manifest(
                "p",
                setup=PluginSetup(providers=(SetupProvider(id="anthropic"),)),
            )
        )
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=(),
            registered_channels=(),
            registered_providers=("anthropic", "rogue"),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("rogue" in d and "registered but not declared" in d for d in shape.drift)

    def test_declared_but_unregistered_provider_in_drift(self) -> None:
        from plugin_sdk.core import PluginSetup, SetupProvider

        cand = _candidate(
            _make_manifest(
                "p",
                setup=PluginSetup(
                    providers=(
                        SetupProvider(id="anthropic"),
                        SetupProvider(id="missing"),
                    )
                ),
            )
        )
        shape = inspect_shape_from_candidate(
            cand,
            registered_tools=(),
            registered_channels=(),
            registered_providers=("anthropic",),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("missing" in d and "declared but not registered" in d for d in shape.drift)


class TestInspectShapeLive:
    def test_unknown_plugin_returns_drift_shape(self) -> None:
        shape = inspect_shape("does-not-exist-xyz-0123456789")
        assert isinstance(shape, PluginShape)
        assert shape.classification == "drift"
        # Drift message should reference "not loaded" or "not found".
        assert shape.drift, "expected at least one drift message"
        joined = " ".join(shape.drift).lower()
        assert "not loaded" in joined or "not found" in joined or "no candidate" in joined

    def test_already_loaded_plugin_uses_cached_registrations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the plugin is already in PluginRegistry.loaded, use those
        registrations directly without calling load_plugin again."""
        from opencomputer.plugins import inspect_shape as ish_mod
        from opencomputer.plugins.loader import LoadedPlugin, PluginRegistrations

        cand = _candidate(_make_manifest("preloaded", tool_names=("FakeTool",)))

        # Stub discover() to return our synthetic candidate.
        monkeypatch.setattr(ish_mod, "discover", lambda paths: [cand])

        # Pretend the plugin is already loaded with matching registrations.
        loaded = LoadedPlugin(
            candidate=cand,
            module=object(),
            registrations=PluginRegistrations(tool_names=("FakeTool",)),
        )
        from opencomputer.plugins.registry import registry as plugin_registry

        original_loaded_list = list(plugin_registry.loaded)
        plugin_registry.loaded.append(loaded)
        try:
            shape = inspect_shape("preloaded")
            assert shape.classification == "valid"
            assert shape.actual_tools == ("FakeTool",)
        finally:
            plugin_registry.loaded[:] = original_loaded_list

    def test_load_failure_surfaced_as_drift(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When load_plugin raises, inspect_shape captures the error
        in drift and never re-raises."""
        from opencomputer.plugins import inspect_shape as ish_mod

        cand = _candidate(_make_manifest("crashy", tool_names=("X",)))

        # Stub discover() to return our synthetic candidate.
        monkeypatch.setattr(ish_mod, "discover", lambda paths: [cand])

        # Stub load_plugin to raise. We patch via the module attribute
        # used during the lazy import inside inspect_shape().
        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthetic boom")

        import opencomputer.plugins.loader as loader_mod

        monkeypatch.setattr(loader_mod, "load_plugin", _boom)

        shape = inspect_shape("crashy")
        assert shape.classification == "drift"
        assert any("load failed" in d for d in shape.drift)
        assert any("synthetic boom" in d for d in shape.drift)
