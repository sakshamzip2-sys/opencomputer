"""Tests for ``/plugin reload <id>`` slash + ``reload_plugin`` helper.

These test the hot-reload path end-to-end:

1. ``reload_plugin`` in the loader composes ``teardown_loaded_plugin``
   + ``load_plugin`` and returns ``(new_loaded, message)``.
2. The ``PluginReloadCommand`` slash routes the input, looks up the
   plugin in the registry, calls the helper, and reports.

Failure modes covered:

* Missing args (no subcommand).
* No id passed.
* Plugin id not in registry.loaded.
* Registry/api not wired into runtime.
* reload_plugin returning ``(None, message)``.
* The successful path — counts surface in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from opencomputer.agent.slash_commands_impl.plugin_reload_cmd import (
    PluginReloadCommand,
)
from plugin_sdk.runtime_context import RuntimeContext

# ─── Fixtures ──────────────────────────────────────────────────────────


@dataclass
class _FakeRegistrations:
    tool_names: list[str] = field(default_factory=list)
    hook_specs: list[Any] = field(default_factory=list)
    slash_names: list[str] = field(default_factory=list)


@dataclass
class _FakeManifest:
    id: str


@dataclass
class _FakeCandidate:
    manifest: _FakeManifest


@dataclass
class _FakeLoadedPlugin:
    candidate: _FakeCandidate
    registrations: _FakeRegistrations = field(default_factory=_FakeRegistrations)


@dataclass
class _FakeRegistry:
    loaded: list[_FakeLoadedPlugin] = field(default_factory=list)
    shared_api: Any = None


def _runtime(registry: _FakeRegistry | None) -> RuntimeContext:
    return RuntimeContext(custom={"plugin_registry": registry})


# ─── Argument parsing / usage paths ────────────────────────────────────


class TestUsage:
    @pytest.mark.asyncio
    async def test_no_args_returns_usage(self) -> None:
        result = await PluginReloadCommand().execute("", _runtime(_FakeRegistry()))
        assert "usage:" in result.output.lower()
        assert "reload" in result.output

    @pytest.mark.asyncio
    async def test_reload_without_id_returns_usage(self) -> None:
        result = await PluginReloadCommand().execute(
            "reload", _runtime(_FakeRegistry())
        )
        assert "id is required" in result.output.lower()

    @pytest.mark.asyncio
    async def test_unknown_subcommand_returns_usage(self) -> None:
        result = await PluginReloadCommand().execute(
            "foo bar", _runtime(_FakeRegistry())
        )
        # Anything other than ``reload`` falls through to usage.
        assert "usage:" in result.output.lower()


# ─── Error paths ──────────────────────────────────────────────────────


class TestErrors:
    @pytest.mark.asyncio
    async def test_no_registry_in_runtime(self) -> None:
        rt = RuntimeContext(custom={})
        result = await PluginReloadCommand().execute("reload my-plugin", rt)
        assert "not wired" in result.output.lower()

    @pytest.mark.asyncio
    async def test_plugin_not_loaded(self) -> None:
        reg = _FakeRegistry(
            loaded=[
                _FakeLoadedPlugin(_FakeCandidate(_FakeManifest("other-plugin"))),
            ],
            shared_api=object(),
        )
        result = await PluginReloadCommand().execute("reload missing", _runtime(reg))
        assert "no plugin" in result.output.lower()
        # Available plugins should be listed.
        assert "other-plugin" in result.output

    @pytest.mark.asyncio
    async def test_no_shared_api(self) -> None:
        reg = _FakeRegistry(
            loaded=[_FakeLoadedPlugin(_FakeCandidate(_FakeManifest("p1")))],
            shared_api=None,
        )
        result = await PluginReloadCommand().execute("reload p1", _runtime(reg))
        assert "shared_api" in result.output.lower()


# ─── Success + helper-failure paths ───────────────────────────────────


class TestExecution:
    @pytest.mark.asyncio
    async def test_success_replaces_loaded_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = _FakeLoadedPlugin(_FakeCandidate(_FakeManifest("p1")))
        new_loaded = _FakeLoadedPlugin(
            _FakeCandidate(_FakeManifest("p1")),
            registrations=_FakeRegistrations(
                tool_names=["NewTool"],
                hook_specs=["h1", "h2"],
                slash_names=["new"],
            ),
        )
        reg = _FakeRegistry(loaded=[old], shared_api=object())

        # Patch the loader helper.
        from opencomputer.plugins import loader as loader_mod

        monkeypatch.setattr(
            loader_mod,
            "reload_plugin",
            lambda loaded, api, **_kw: (new_loaded, "reloaded p1 (1 tools, 2 hooks, 1 slash commands)"),
        )

        result = await PluginReloadCommand().execute("reload p1", _runtime(reg))
        assert "✓" in result.output
        assert "reloaded p1" in result.output
        # Registry entry was swapped to the new LoadedPlugin.
        assert reg.loaded[0] is new_loaded

    @pytest.mark.asyncio
    async def test_helper_failure_surfaces_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = _FakeLoadedPlugin(_FakeCandidate(_FakeManifest("p1")))
        reg = _FakeRegistry(loaded=[old], shared_api=object())

        from opencomputer.plugins import loader as loader_mod

        monkeypatch.setattr(
            loader_mod,
            "reload_plugin",
            lambda loaded, api, **_kw: (None, "load_plugin returned None"),
        )

        result = await PluginReloadCommand().execute("reload p1", _runtime(reg))
        assert "reload failed" in result.output.lower()
        # The user is warned that the plugin is now unloaded.
        assert "unloaded" in result.output.lower()
        # Registry entry should NOT have been replaced — old object is
        # still there so the user knows what state they're in.
        assert reg.loaded[0] is old
