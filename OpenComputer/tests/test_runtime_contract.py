"""Task I.5 — Runtime contract validation after ``register(api)``.

The loader inspects what a plugin's ``register(api)`` actually registered
with the ``PluginAPI`` and compares against the manifest's declared
``kind`` (plus ``tool_names`` if declared). A claim-vs-reality mismatch
emits a WARNING — it does NOT block load.

Mirrors OpenClaw's pattern: ``sources/openclaw/src/plugins/manifest.ts``
``contracts`` field + loader validation in
``sources/openclaw/src/plugins/loader.ts``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from opencomputer.agent.injection import InjectionEngine
from opencomputer.hooks.engine import HookEngine
from opencomputer.plugins.discovery import PluginCandidate
from opencomputer.plugins.loader import PluginAPI, load_plugin
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import PluginManifest
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _FakeTool(BaseTool):
    """Minimal BaseTool used to exercise api.register_tool."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._name,
            description=f"fake tool {self._name}",
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, call):  # pragma: no cover — not exercised
        raise NotImplementedError


def _isolated_api(tmp_path: Path) -> PluginAPI:
    """Fresh PluginAPI with isolated registries for one plugin."""
    return PluginAPI(
        tool_registry=ToolRegistry(),
        hook_engine=HookEngine(),
        provider_registry={},
        channel_registry={},
        injection_engine=InjectionEngine(),
        doctor_contributions=[],
        session_db_path=tmp_path / "session.sqlite",
    )


def _write_entry(root: Path, entry_name: str, body: str) -> None:
    """Scaffold an entry module with the given ``register(api)`` body."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text("{}", encoding="utf-8")
    (root / f"{entry_name}.py").write_text(body, encoding="utf-8")


def _candidate(
    root: Path,
    entry: str,
    plugin_id: str,
    *,
    kind: str = "mixed",
    tool_names: tuple[str, ...] = (),
) -> PluginCandidate:
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.0.1",
        kind=kind,  # type: ignore[arg-type]
        entry=entry,
        tool_names=tool_names,
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=root,
        manifest_path=root / "plugin.json",
    )


# ─── kind=provider ─────────────────────────────────────────────────────


def test_provider_kind_without_any_provider_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """kind=provider + plugin registers nothing → WARNING about empty claim."""
    root = tmp_path / "prov-empty"
    _write_entry(
        root,
        "entry_mod",
        "def register(api):\n    pass\n",
    )
    cand = _candidate(root, "entry_mod", "prov-empty", kind="provider")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    # Load still succeeded — we WARN, we don't block.
    assert loaded is not None
    # Warning names the plugin id, the declared kind, and the fact that
    # nothing matching was registered.
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "prov-empty" in m and "provider" in m and "registered no" in m
        for m in msgs
    ), f"expected contract-violation warning; got: {msgs}"


def test_provider_kind_with_one_provider_registered_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """kind=provider + plugin registers one provider → no contract warning."""
    root = tmp_path / "prov-ok"
    _write_entry(
        root,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_provider('fake', object())\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "prov-ok", kind="provider")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("registered no provider" in m for m in msgs), (
        f"unexpected contract warning: {msgs}"
    )


def test_provider_kind_accepts_memory_provider_as_provider(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """kind=provider is broad — a memory provider satisfies the provider claim.

    Matches the bundled ``memory-honcho`` plugin which declares
    kind=provider and registers via ``register_memory_provider``.
    """
    root = tmp_path / "memo-as-prov"
    # Use a MemoryProvider stub that implements every abstract method
    # with a trivial return — we only need registration to succeed.
    _write_entry(
        root,
        "entry_mod",
        (
            "from plugin_sdk.memory import MemoryProvider\n"
            "class M(MemoryProvider):\n"
            "    @property\n"
            "    def provider_id(self): return 'demo-memo'\n"
            "    def tool_schemas(self): return []\n"
            "    async def handle_tool_call(self, call): return None\n"
            "    async def prefetch(self, query, turn_index): return None\n"
            "    async def sync_turn(self, user, assistant, turn_index): return None\n"
            "    async def health_check(self): return True\n"
            "def register(api):\n"
            "    api.register_memory_provider(M())\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "memo-as-prov", kind="provider")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any(
        "registered no provider" in m and "memo-as-prov" in m for m in msgs
    ), f"memory provider should satisfy kind=provider; got: {msgs}"


# ─── kind=channel ──────────────────────────────────────────────────────


def test_channel_kind_without_any_channel_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "chan-empty"
    _write_entry(
        root,
        "entry_mod",
        "def register(api):\n    pass\n",
    )
    cand = _candidate(root, "entry_mod", "chan-empty", kind="channel")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "chan-empty" in m and "channel" in m and "registered no" in m
        for m in msgs
    ), f"expected contract-violation warning; got: {msgs}"


def test_channel_kind_with_adapter_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "chan-ok"
    _write_entry(
        root,
        "entry_mod",
        (
            "def register(api):\n"
            "    api.register_channel('fake', object())\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "chan-ok", kind="channel")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("registered no channel" in m for m in msgs), (
        f"unexpected contract warning: {msgs}"
    )


# ─── kind=tool ─────────────────────────────────────────────────────────


def test_tool_kind_without_any_tool_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "tool-empty"
    _write_entry(
        root,
        "entry_mod",
        "def register(api):\n    pass\n",
    )
    cand = _candidate(root, "entry_mod", "tool-empty", kind="tool")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "tool-empty" in m and "tool" in m and "registered no" in m for m in msgs
    ), f"expected contract-violation warning; got: {msgs}"


# ─── kind=mixed ────────────────────────────────────────────────────────


def test_mixed_kind_with_one_tool_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """kind=mixed + at least 1 registration of any kind → no warning."""
    root = tmp_path / "mix-tool"
    _write_entry(
        root,
        "entry_mod",
        (
            "from plugin_sdk.tool_contract import BaseTool, ToolSchema\n"
            "class T(BaseTool):\n"
            "    @property\n"
            "    def schema(self):\n"
            "        return ToolSchema(name='Foo', description='x',\n"
            "            parameters={'type':'object','properties':{},'required':[]})\n"
            "    async def execute(self, call):\n"
            "        return None\n"
            "def register(api):\n"
            "    api.register_tool(T())\n"
        ),
    )
    cand = _candidate(root, "entry_mod", "mix-tool", kind="mixed")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("registered no" in m and "mix-tool" in m for m in msgs), (
        f"unexpected contract warning: {msgs}"
    )


def test_mixed_kind_with_nothing_registered_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """kind=mixed + plugin registers literally nothing → WARNING.

    A mixed plugin that registered NOTHING is almost certainly a bug
    (copy-pasted stub, broken import). Warn loudly.
    """
    root = tmp_path / "mix-empty"
    _write_entry(
        root,
        "entry_mod",
        "def register(api):\n    pass\n",
    )
    cand = _candidate(root, "entry_mod", "mix-empty", kind="mixed")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "mix-empty" in m and "mixed" in m and "registered no" in m for m in msgs
    ), f"expected contract-violation warning; got: {msgs}"


# ─── kind=skill ────────────────────────────────────────────────────────


def test_skill_kind_without_runtime_registrations_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """kind=skill plugins register files, not runtime items — no warning expected."""
    root = tmp_path / "skill-only"
    _write_entry(
        root,
        "entry_mod",
        "def register(api):\n    pass\n",
    )
    cand = _candidate(root, "entry_mod", "skill-only", kind="skill")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("registered no" in m and "skill-only" in m for m in msgs), (
        f"skill plugins shouldn't trigger contract warning: {msgs}"
    )


# ─── manifest.tool_names contract ──────────────────────────────────────


def test_tool_names_matching_registered_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """tool_names=['Foo','Bar'] + plugin registers a tool named 'Foo' → no warning."""
    root = tmp_path / "tn-match"
    _write_entry(
        root,
        "entry_mod",
        (
            "from plugin_sdk.tool_contract import BaseTool, ToolSchema\n"
            "class T(BaseTool):\n"
            "    @property\n"
            "    def schema(self):\n"
            "        return ToolSchema(name='Foo', description='x',\n"
            "            parameters={'type':'object','properties':{},'required':[]})\n"
            "    async def execute(self, call):\n"
            "        return None\n"
            "def register(api):\n"
            "    api.register_tool(T())\n"
        ),
    )
    cand = _candidate(
        root,
        "entry_mod",
        "tn-match",
        kind="tool",
        tool_names=("Foo", "Bar"),
    )

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any(
        "tool_names" in m and "tn-match" in m for m in msgs
    ), f"unexpected tool_names warning: {msgs}"


def test_tool_names_mismatch_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """tool_names=['Foo'] + plugin registers 'Other' → WARNING naming the mismatch."""
    root = tmp_path / "tn-mismatch"
    _write_entry(
        root,
        "entry_mod",
        (
            "from plugin_sdk.tool_contract import BaseTool, ToolSchema\n"
            "class T(BaseTool):\n"
            "    @property\n"
            "    def schema(self):\n"
            "        return ToolSchema(name='Other', description='x',\n"
            "            parameters={'type':'object','properties':{},'required':[]})\n"
            "    async def execute(self, call):\n"
            "        return None\n"
            "def register(api):\n"
            "    api.register_tool(T())\n"
        ),
    )
    cand = _candidate(
        root,
        "entry_mod",
        "tn-mismatch",
        kind="tool",
        tool_names=("Foo",),
    )

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "tn-mismatch" in m and "tool_names" in m for m in msgs
    ), f"expected tool_names contract warning; got: {msgs}"


# ─── WARN not ERROR (does not block load) ──────────────────────────────


def test_contract_violation_does_not_block_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A declared-but-empty plugin still loads; we only warn."""
    root = tmp_path / "still-loads"
    _write_entry(
        root,
        "entry_mod",
        "MARKER = 'loaded'\ndef register(api):\n    pass\n",
    )
    cand = _candidate(root, "entry_mod", "still-loads", kind="provider")

    caplog.set_level(logging.WARNING, logger="opencomputer.plugins.loader")
    loaded = load_plugin(cand, _isolated_api(tmp_path))

    assert loaded is not None
    assert loaded.module.MARKER == "loaded"
