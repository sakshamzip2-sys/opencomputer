"""inspect_shape — compare manifest claims vs actual plugin registrations.

Sub-project G (openclaw-parity) Task 7. Pure helper
``inspect_shape_from_candidate`` is unit-tested with synthetic registry
data; the live ``inspect_shape(plugin_id)`` is exercised by Task 12's
end-to-end smoke test.
"""

from __future__ import annotations

from pathlib import Path

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
        setup=None,
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


class TestInspectShapeLive:
    def test_unknown_plugin_returns_drift_shape(self) -> None:
        shape = inspect_shape("does-not-exist-xyz-0123456789")
        assert isinstance(shape, PluginShape)
        assert shape.classification == "drift"
        # Drift message should reference "not loaded" or "not found".
        assert shape.drift, "expected at least one drift message"
        joined = " ".join(shape.drift).lower()
        assert "not loaded" in joined or "not found" in joined or "no candidate" in joined
