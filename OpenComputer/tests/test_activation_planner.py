"""Activation planner — derive activation list from manifest triggers.

Sub-project G (openclaw-parity) Task 6. Pure function — no filesystem
I/O, no plugin loading. Reads PluginManifest.activation + tool_names
and a snapshot of triggers; returns deterministic plugin id list.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.plugins.activation_planner import (
    ActivationTriggers,
    plan_activations,
)
from opencomputer.plugins.discovery import PluginCandidate
from plugin_sdk.core import PluginActivation, PluginManifest


def _make_candidate(
    plugin_id: str,
    *,
    activation: PluginActivation | None = None,
    tool_names: tuple[str, ...] = (),
) -> PluginCandidate:
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        description="",
        author="",
        homepage="",
        license="MIT",
        kind="tool",
        entry="plugin",
        profiles=None,
        single_instance=False,
        enabled_by_default=False,
        tool_names=tool_names,
        optional_tool_names=(),
        mcp_servers=(),
        model_support=None,
        legacy_plugin_ids=(),
        setup=None,
        min_host_version="",
        activation=activation,
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=Path("/tmp/fake"),
        manifest_path=Path("/tmp/fake/plugin.json"),
    )


class TestActivationPlanner:
    def test_no_triggers_no_activations(self) -> None:
        cands = [_make_candidate("a", activation=PluginActivation(on_providers=("openai",)))]
        result = plan_activations(cands, ActivationTriggers())
        assert result == []

    def test_provider_trigger_activates_match(self) -> None:
        cands = [
            _make_candidate("a", activation=PluginActivation(on_providers=("anthropic",))),
            _make_candidate("b", activation=PluginActivation(on_providers=("openai",))),
        ]
        result = plan_activations(
            cands,
            ActivationTriggers(active_providers=frozenset({"anthropic"})),
        )
        assert result == ["a"]

    def test_multiple_triggers_dedup(self) -> None:
        cands = [
            _make_candidate(
                "a",
                activation=PluginActivation(
                    on_providers=("anthropic",), on_tools=("X",)
                ),
            ),
        ]
        result = plan_activations(
            cands,
            ActivationTriggers(
                active_providers=frozenset({"anthropic"}),
                requested_tools=frozenset({"X"}),
            ),
        )
        assert result == ["a"]

    def test_legacy_tool_names_path_when_activation_absent(self) -> None:
        cands = [_make_candidate("legacy", tool_names=("LegacyTool",))]
        result = plan_activations(
            cands,
            ActivationTriggers(requested_tools=frozenset({"LegacyTool"})),
        )
        assert result == ["legacy"]

    def test_activation_unions_with_tool_names(self) -> None:
        cands = [
            _make_candidate(
                "modern",
                activation=PluginActivation(on_tools=("ModernTool",)),
                tool_names=("AlsoLegacy",),
            )
        ]
        r1 = plan_activations(
            cands,
            ActivationTriggers(requested_tools=frozenset({"ModernTool"})),
        )
        assert r1 == ["modern"]
        r2 = plan_activations(
            cands,
            ActivationTriggers(requested_tools=frozenset({"AlsoLegacy"})),
        )
        assert r2 == ["modern"]

    def test_command_trigger(self) -> None:
        cands = [_make_candidate("a", activation=PluginActivation(on_commands=("/foo",)))]
        r = plan_activations(cands, ActivationTriggers(invoked_commands=frozenset({"/foo"})))
        assert r == ["a"]

    def test_channel_trigger(self) -> None:
        cands = [
            _make_candidate("a", activation=PluginActivation(on_channels=("telegram",)))
        ]
        r = plan_activations(cands, ActivationTriggers(active_channels=frozenset({"telegram"})))
        assert r == ["a"]

    def test_model_prefix_trigger(self) -> None:
        cands = [
            _make_candidate("a", activation=PluginActivation(on_models=("claude-",)))
        ]
        r = plan_activations(cands, ActivationTriggers(active_model="claude-opus-4-7"))
        assert r == ["a"]

    def test_result_sorted_deterministic(self) -> None:
        cands = [
            _make_candidate("zebra", activation=PluginActivation(on_providers=("x",))),
            _make_candidate("apple", activation=PluginActivation(on_providers=("x",))),
        ]
        r = plan_activations(cands, ActivationTriggers(active_providers=frozenset({"x"})))
        assert r == ["apple", "zebra"]

    def test_empty_active_model_skips_on_models(self) -> None:
        cands = [
            _make_candidate("a", activation=PluginActivation(on_models=("claude-",)))
        ]
        r = plan_activations(cands, ActivationTriggers(active_model=""))
        assert r == []
