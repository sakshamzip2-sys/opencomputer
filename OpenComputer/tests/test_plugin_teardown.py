"""Task I.4 — Per-plugin module-state teardown.

``PluginRegistry.teardown_plugin(plugin_id)`` tears down a single loaded
plugin by:

1. Calling the plugin's optional ``cleanup()`` / ``teardown()`` entry-point
   if present.
2. Removing the plugin's registered items from the PluginAPI (tools,
   providers, channels, slash commands, injection providers, hooks,
   memory provider, doctor contributions).
3. Removing the synthetic + common sibling modules from ``sys.modules``
   so a subsequent reload sees a fresh module graph.

The per-plugin registration set is computed via the snapshot-before /
snapshot-after mechanism introduced for I.5 — the loader stores the
delta on the ``LoadedPlugin`` so teardown knows exactly which entries
belong to THIS plugin, even when multiple plugins contributed to the
same registry dict.

Mirrors OpenClaw's ``clearPluginLoaderCache`` pattern
(``sources/openclaw/src/plugins/loader.ts:222-230``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from opencomputer.agent.injection import InjectionEngine
from opencomputer.hooks.engine import HookEngine
from opencomputer.plugins.discovery import PluginCandidate
from opencomputer.plugins.loader import PluginAPI, load_plugin
from opencomputer.plugins.registry import PluginRegistry
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import PluginManifest


def _isolated_api(
    tmp_path: Path,
    *,
    tool_registry: ToolRegistry | None = None,
    hook_engine: HookEngine | None = None,
    providers: dict[str, Any] | None = None,
    channels: dict[str, Any] | None = None,
    injection_engine: InjectionEngine | None = None,
    doctor_contributions: list[Any] | None = None,
    slash_commands: dict[str, Any] | None = None,
) -> PluginAPI:
    """Build a fresh PluginAPI with isolated registries."""
    return PluginAPI(
        tool_registry=tool_registry if tool_registry is not None else ToolRegistry(),
        hook_engine=hook_engine if hook_engine is not None else HookEngine(),
        provider_registry=providers if providers is not None else {},
        channel_registry=channels if channels is not None else {},
        injection_engine=(
            injection_engine if injection_engine is not None else InjectionEngine()
        ),
        doctor_contributions=(
            doctor_contributions if doctor_contributions is not None else []
        ),
        session_db_path=tmp_path / "session.sqlite",
        slash_commands=slash_commands if slash_commands is not None else {},
    )


def _write_entry(root: Path, entry_name: str, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text("{}", encoding="utf-8")
    (root / f"{entry_name}.py").write_text(body, encoding="utf-8")


def _candidate(
    root: Path,
    entry: str,
    plugin_id: str,
    *,
    kind: str = "mixed",
) -> PluginCandidate:
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.0.1",
        kind=kind,  # type: ignore[arg-type]
        entry=entry,
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=root,
        manifest_path=root / "plugin.json",
    )


# ─── 1. teardown on missing id is a no-op ──────────────────────────────


def test_teardown_nonexistent_plugin_is_noop(tmp_path: Path) -> None:
    """``teardown_plugin('does-not-exist')`` must not raise or mutate state."""
    reg = PluginRegistry()
    # Pre-populate with some sibling state to verify it's untouched.
    reg.providers["other"] = object()
    reg.channels["other-chan"] = object()
    reg.slash_commands["other-slash"] = object()

    # Should return False (nothing torn down) and not raise.
    result = reg.teardown_plugin("does-not-exist")
    assert result is False

    assert "other" in reg.providers
    assert "other-chan" in reg.channels
    assert "other-slash" in reg.slash_commands


# ─── 2. teardown removes a plugin's tool registration ──────────────────


def test_teardown_removes_tool_registered_by_plugin(tmp_path: Path) -> None:
    """Load a plugin that registers 1 tool → teardown → tool is gone."""
    root = tmp_path / "td-one-tool"
    _write_entry(
        root,
        "entry_mod",
        (
            "from plugin_sdk.tool_contract import BaseTool, ToolSchema\n"
            "class T(BaseTool):\n"
            "    @property\n"
            "    def schema(self):\n"
            "        return ToolSchema(name='TeardownOne', description='x',\n"
            "            parameters={'type':'object','properties':{},'required':[]})\n"
            "    async def execute(self, call):\n"
            "        return None\n"
            "def register(api):\n"
            "    api.register_tool(T())\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "td-one-tool")

    tool_reg = ToolRegistry()
    api = _isolated_api(tmp_path, tool_registry=tool_reg)

    reg = PluginRegistry()
    loaded = load_plugin(cand, api)
    assert loaded is not None
    reg.loaded.append(loaded)

    assert tool_reg.get("TeardownOne") is not None

    ok = reg.teardown_plugin("td-one-tool")
    assert ok is True
    assert tool_reg.get("TeardownOne") is None
    # LoadedPlugin entry is removed from registry.loaded.
    assert all(lp.candidate.manifest.id != "td-one-tool" for lp in reg.loaded)


# ─── 3. teardown calls the plugin's cleanup() hook exactly once ────────


def test_teardown_calls_cleanup_hook_once(tmp_path: Path) -> None:
    """Load a plugin that defines cleanup() → teardown → cleanup called once."""
    root = tmp_path / "td-cleanup"
    _write_entry(
        root,
        "entry_mod",
        (
            "CLEANUP_CALLS = []\n"
            "def register(api):\n"
            "    pass\n"
            "def cleanup():\n"
            "    CLEANUP_CALLS.append(1)\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "td-cleanup")

    api = _isolated_api(tmp_path)
    reg = PluginRegistry()
    loaded = load_plugin(cand, api)
    assert loaded is not None
    reg.loaded.append(loaded)

    assert loaded.module.CLEANUP_CALLS == []
    module_ref = loaded.module  # keep ref so we can assert afterward

    ok = reg.teardown_plugin("td-cleanup")
    assert ok is True
    # cleanup() ran exactly once before the module was dropped.
    assert module_ref.CLEANUP_CALLS == [1]


def test_teardown_calls_teardown_hook_if_no_cleanup(tmp_path: Path) -> None:
    """If the plugin defines teardown() (not cleanup), teardown() is called."""
    root = tmp_path / "td-teardown-fn"
    _write_entry(
        root,
        "entry_mod",
        (
            "TEARDOWN_CALLS = []\n"
            "def register(api):\n"
            "    pass\n"
            "def teardown():\n"
            "    TEARDOWN_CALLS.append('x')\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "td-teardown-fn")

    api = _isolated_api(tmp_path)
    reg = PluginRegistry()
    loaded = load_plugin(cand, api)
    assert loaded is not None
    reg.loaded.append(loaded)

    module_ref = loaded.module

    ok = reg.teardown_plugin("td-teardown-fn")
    assert ok is True
    assert module_ref.TEARDOWN_CALLS == ["x"]


def test_teardown_without_cleanup_hook_still_succeeds(tmp_path: Path) -> None:
    """Plugin without cleanup()/teardown() still tears down the registry."""
    root = tmp_path / "td-no-hook"
    _write_entry(
        root,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_provider('td-prov', object())\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "td-no-hook")

    providers: dict[str, Any] = {}
    api = _isolated_api(tmp_path, providers=providers)
    reg = PluginRegistry(providers=providers)
    loaded = load_plugin(cand, api)
    assert loaded is not None
    reg.loaded.append(loaded)

    assert "td-prov" in providers

    ok = reg.teardown_plugin("td-no-hook")
    assert ok is True
    assert "td-prov" not in providers


# ─── 4. reload after teardown succeeds cleanly ─────────────────────────


def test_reload_after_teardown_succeeds(tmp_path: Path) -> None:
    """After teardown, loading the same plugin again works (no leftover state)."""
    root = tmp_path / "td-reload"
    _write_entry(
        root,
        "entry_mod",
        (
            "from plugin_sdk.tool_contract import BaseTool, ToolSchema\n"
            "class T(BaseTool):\n"
            "    @property\n"
            "    def schema(self):\n"
            "        return ToolSchema(name='ReloadOne', description='x',\n"
            "            parameters={'type':'object','properties':{},'required':[]})\n"
            "    async def execute(self, call):\n"
            "        return None\n"
            "def register(api):\n"
            "    api.register_tool(T())\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "td-reload")

    tool_reg = ToolRegistry()
    api = _isolated_api(tmp_path, tool_registry=tool_reg)
    reg = PluginRegistry()

    loaded1 = load_plugin(cand, api)
    assert loaded1 is not None
    reg.loaded.append(loaded1)
    assert tool_reg.get("ReloadOne") is not None

    # Teardown.
    ok = reg.teardown_plugin("td-reload")
    assert ok is True
    assert tool_reg.get("ReloadOne") is None

    # Reload — should NOT raise "tool already registered" or similar.
    loaded2 = load_plugin(cand, api)
    assert loaded2 is not None
    reg.loaded.append(loaded2)
    assert tool_reg.get("ReloadOne") is not None

    # The synthetic module name should be back in sys.modules (loader writes it).
    synth_name = "_opencomputer_plugin_td_reload_entry_mod"
    assert synth_name in sys.modules


# ─── 5. teardown removes the synthetic module from sys.modules ─────────


def test_teardown_removes_synthetic_module(tmp_path: Path) -> None:
    """After teardown, the plugin's synthetic module is gone from sys.modules."""
    root = tmp_path / "td-sysmod"
    _write_entry(
        root,
        "entry_mod",
        "def register(api):\n    pass\n",
    )
    cand = _candidate(root, "entry_mod", "td-sysmod")

    api = _isolated_api(tmp_path)
    reg = PluginRegistry()
    loaded = load_plugin(cand, api)
    assert loaded is not None
    reg.loaded.append(loaded)

    synth_name = "_opencomputer_plugin_td_sysmod_entry_mod"
    assert synth_name in sys.modules

    ok = reg.teardown_plugin("td-sysmod")
    assert ok is True
    assert synth_name not in sys.modules


# ─── 6. teardown removes all tracked registration kinds ────────────────


def test_teardown_removes_all_registration_kinds(tmp_path: Path) -> None:
    """A plugin registering every kind sees all its entries removed on teardown."""
    root = tmp_path / "td-all-kinds"
    _write_entry(
        root,
        "entry_mod",
        (
            "from plugin_sdk.tool_contract import BaseTool, ToolSchema\n"
            "from plugin_sdk.hooks import HookSpec, HookEvent\n"
            "from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext\n"
            "class T(BaseTool):\n"
            "    @property\n"
            "    def schema(self):\n"
            "        return ToolSchema(name='AllKindsTool', description='x',\n"
            "            parameters={'type':'object','properties':{},'required':[]})\n"
            "    async def execute(self, call):\n"
            "        return None\n"
            "class Slash:\n"
            "    name = 'all-kinds-slash'\n"
            "    description = 'd'\n"
            "    async def execute(self, args, runtime):\n"
            "        return None\n"
            "class Inj(DynamicInjectionProvider):\n"
            "    provider_id = 'all-kinds-inj'\n"
            "    priority = 100\n"
            "    async def collect(self, ctx):\n"
            "        return ''\n"
            "async def _h(ctx):\n"
            "    return None\n"
            "def register(api):\n"
            "    api.register_tool(T())\n"
            "    api.register_provider('all-kinds-prov', object())\n"
            "    api.register_channel('all-kinds-chan', object())\n"
            "    api.register_slash_command(Slash())\n"
            "    api.register_injection_provider(Inj())\n"
            "    api.register_hook(HookSpec(event=HookEvent.POST_TOOL_USE, handler=_h))\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "td-all-kinds")

    tool_reg = ToolRegistry()
    hook_eng = HookEngine()
    inj_eng = InjectionEngine()
    providers: dict[str, Any] = {}
    channels: dict[str, Any] = {}
    slash: dict[str, Any] = {}
    api = _isolated_api(
        tmp_path,
        tool_registry=tool_reg,
        hook_engine=hook_eng,
        providers=providers,
        channels=channels,
        injection_engine=inj_eng,
        slash_commands=slash,
    )
    reg = PluginRegistry(
        providers=providers,
        channels=channels,
        slash_commands=slash,
    )
    loaded = load_plugin(cand, api)
    assert loaded is not None
    reg.loaded.append(loaded)

    # Sanity — everything got registered.
    assert tool_reg.get("AllKindsTool") is not None
    assert "all-kinds-prov" in providers
    assert "all-kinds-chan" in channels
    assert "all-kinds-slash" in slash
    assert any(
        p.provider_id == "all-kinds-inj" for p in inj_eng.providers()
    )
    from plugin_sdk.hooks import HookEvent

    hook_count_before = sum(len(v) for v in hook_eng._hooks.values())
    assert hook_count_before >= 1
    # The registered hook is under POST_TOOL_USE.
    assert len(hook_eng._hooks[HookEvent.POST_TOOL_USE]) == 1

    ok = reg.teardown_plugin("td-all-kinds")
    assert ok is True

    assert tool_reg.get("AllKindsTool") is None
    assert "all-kinds-prov" not in providers
    assert "all-kinds-chan" not in channels
    assert "all-kinds-slash" not in slash
    assert not any(
        p.provider_id == "all-kinds-inj" for p in inj_eng.providers()
    )
    # Hook for POST_TOOL_USE should be gone.
    assert len(hook_eng._hooks.get(HookEvent.POST_TOOL_USE, [])) == 0


def test_teardown_preserves_sibling_plugin_registrations(tmp_path: Path) -> None:
    """Two plugins contributing to the same dict — teardown removes only A's."""
    root_a = tmp_path / "td-sib-a"
    _write_entry(
        root_a,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_provider('sib-a-prov', object())\n"
            "    api.register_channel('sib-a-chan', object())\n"
        ),
    )
    root_b = tmp_path / "td-sib-b"
    _write_entry(
        root_b,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_provider('sib-b-prov', object())\n"
            "    api.register_channel('sib-b-chan', object())\n"
        ),
    )

    providers: dict[str, Any] = {}
    channels: dict[str, Any] = {}
    api = _isolated_api(tmp_path, providers=providers, channels=channels)
    reg = PluginRegistry(providers=providers, channels=channels)

    cand_a = _candidate(root_a, "entry_mod", "td-sib-a")
    cand_b = _candidate(root_b, "entry_mod", "td-sib-b")

    loaded_a = load_plugin(cand_a, api)
    loaded_b = load_plugin(cand_b, api)
    assert loaded_a is not None and loaded_b is not None
    reg.loaded.extend([loaded_a, loaded_b])

    assert "sib-a-prov" in providers and "sib-b-prov" in providers
    assert "sib-a-chan" in channels and "sib-b-chan" in channels

    # Tear down A — B's entries must stay.
    ok = reg.teardown_plugin("td-sib-a")
    assert ok is True

    assert "sib-a-prov" not in providers
    assert "sib-a-chan" not in channels
    assert "sib-b-prov" in providers
    assert "sib-b-chan" in channels


# ─── 6b. cross-api teardown handles owned api reference ────────────────


def test_teardown_uses_loaded_plugins_own_api(tmp_path: Path) -> None:
    """Teardown uses the PluginAPI the plugin was loaded with.

    When a registry holds plugins loaded via different PluginAPI
    instances, each ``LoadedPlugin`` carries its own ``api`` reference.
    Teardown dispatches through that reference, so removing plugin A
    (loaded via api_a) doesn't accidentally mutate api_b's state.
    """
    root_a = tmp_path / "td-xapi-a"
    _write_entry(
        root_a,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_provider('xa-prov', object())\n"
        ),
    )
    root_b = tmp_path / "td-xapi-b"
    _write_entry(
        root_b,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_provider('xb-prov', object())\n"
        ),
    )

    providers_a: dict[str, Any] = {}
    providers_b: dict[str, Any] = {}
    api_a = _isolated_api(tmp_path, providers=providers_a)
    api_b = _isolated_api(tmp_path, providers=providers_b)

    reg = PluginRegistry()

    cand_a = _candidate(root_a, "entry_mod", "td-xapi-a")
    cand_b = _candidate(root_b, "entry_mod", "td-xapi-b")

    loaded_a = load_plugin(cand_a, api_a)
    loaded_b = load_plugin(cand_b, api_b)
    assert loaded_a is not None and loaded_b is not None
    reg.loaded.extend([loaded_a, loaded_b])

    assert "xa-prov" in providers_a
    assert "xb-prov" in providers_b

    # Tear down A — api_b's dict must be untouched.
    ok = reg.teardown_plugin("td-xapi-a")
    assert ok is True
    assert "xa-prov" not in providers_a
    assert "xb-prov" in providers_b


# ─── 7. cleanup hook raising does not prevent registry removal ─────────


def test_teardown_survives_cleanup_exception(tmp_path: Path) -> None:
    """If cleanup() raises, registry entries are still removed."""
    root = tmp_path / "td-cleanup-raises"
    _write_entry(
        root,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_provider('raises-prov', object())\n"
            "def cleanup():\n"
            "    raise RuntimeError('boom')\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "td-cleanup-raises")

    providers: dict[str, Any] = {}
    api = _isolated_api(tmp_path, providers=providers)
    reg = PluginRegistry(providers=providers)
    loaded = load_plugin(cand, api)
    assert loaded is not None
    reg.loaded.append(loaded)

    # teardown returns True; cleanup exception is swallowed + logged.
    ok = reg.teardown_plugin("td-cleanup-raises")
    assert ok is True
    assert "raises-prov" not in providers
