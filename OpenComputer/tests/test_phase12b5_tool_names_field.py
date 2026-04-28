"""Phase 12b.5 — Sub-project E, Task E1.

Tests for the new ``PluginManifest.tool_names`` field. This field is the
foundation for E2's ``PluginDemandTracker``, which resolves tool-not-found
events to candidate plugin ids without loading the plugin itself.

Three tests:
  1. Default on ``PluginManifest`` is an empty frozen tuple.
  2. The pydantic schema accepts ``tool_names`` as a list and
     ``_parse_manifest`` round-trips it into a tuple.
  3. Drift-guard: for every bundled extension, the declared
     ``manifest.tool_names`` matches the set of tool schema names that
     ``plugin.register(api)`` actually registers. Fails loudly if a
     plugin author adds a new tool in plugin.py but forgets to update
     plugin.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from opencomputer.agent.injection import InjectionEngine
from opencomputer.hooks.engine import HookEngine
from opencomputer.plugins.discovery import discover
from opencomputer.plugins.loader import PluginAPI, load_plugin
from opencomputer.plugins.manifest_validator import validate_manifest
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import PluginManifest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_DIR = REPO_ROOT / "extensions"


# ─── Test 1: default value on PluginManifest ──────────────────────────


def test_plugin_manifest_default_tool_names_is_empty_tuple() -> None:
    """A PluginManifest without tool_names has the canonical empty tuple."""
    m = PluginManifest(id="x", name="x", version="1.0", entry="p")
    assert m.tool_names == ()
    assert isinstance(m.tool_names, tuple)


# ─── Test 2: schema accepts list, discovery converts to tuple ─────────


def test_manifest_validator_accepts_tool_names_list(tmp_path: Path) -> None:
    """A manifest dict with tool_names validates; discovery returns a tuple."""
    data = {
        "id": "example",
        "name": "Example",
        "version": "0.1.0",
        "entry": "plugin",
        "kind": "tool",
        "tool_names": ["A", "B"],
    }
    schema, err = validate_manifest(data)
    assert err == ""
    assert schema is not None
    assert schema.tool_names == ["A", "B"]

    # End-to-end round-trip via discovery.
    root = tmp_path / "example"
    root.mkdir()
    (root / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")

    cands = discover([tmp_path])
    assert len(cands) == 1
    assert cands[0].manifest.tool_names == ("A", "B")
    assert isinstance(cands[0].manifest.tool_names, tuple)


# ─── Test 3: drift-guard — declared tool_names must match registered ─


def _isolated_api(
    tmp_path: Path,
) -> tuple[PluginAPI, ToolRegistry]:
    """Build a fresh PluginAPI with isolated registries for one plugin."""
    tool_registry = ToolRegistry()
    hook_engine = HookEngine()
    injection_engine = InjectionEngine()
    api = PluginAPI(
        tool_registry=tool_registry,
        hook_engine=hook_engine,
        provider_registry={},
        channel_registry={},
        injection_engine=injection_engine,
        doctor_contributions=[],
        session_db_path=tmp_path / "session.sqlite",
    )
    return api, tool_registry


def test_bundled_plugin_manifests_have_accurate_tool_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Load every bundled plugin and compare declared vs registered tool names.

    This catches the common drift bug: a plugin author adds a new tool
    to ``plugin.py`` via ``api.register_tool(NewTool())`` but forgets to
    update the ``tool_names`` array in ``plugin.json``. Without this
    check, E2's PluginDemandTracker would silently miss the new tool.
    """
    # Sandbox OPENCOMPUTER_HOME so single_instance locks + any plugin
    # that writes to ~/.opencomputer don't touch the real home.
    home = tmp_path / ".opencomputer"
    home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(home))

    # The plugin loader mutates sys.path + sys.modules to make sibling
    # imports work. Snapshot both so we can restore them after — otherwise
    # later tests can pick up the wrong ``provider.py`` from extensions/
    # (multiple plugins have one) and fail spuriously.
    sys_path_snapshot = list(sys.path)
    sys_modules_snapshot = set(sys.modules)

    candidates = discover([EXTENSIONS_DIR])
    assert candidates, "no bundled extensions discovered — test harness broken"

    mismatches: list[str] = []

    try:
        for cand in candidates:
            declared_required: set[str] = set(cand.manifest.tool_names)
            declared_optional: set[str] = set(
                getattr(cand.manifest, "optional_tool_names", ())
            )
            declared_all: set[str] = declared_required | declared_optional

            api, tool_registry = _isolated_api(tmp_path / cand.manifest.id)
            loaded = load_plugin(cand, api)

            if loaded is None:
                # A plugin that fails to load is a distinct problem (missing
                # optional deps, etc.); skip it here — the loader already
                # logged a warning. We don't want a missing optional dep
                # (e.g. playwright for dev-tools Browser) to fail the
                # drift guard when the manifest is genuinely correct.
                continue

            registered: set[str] = set(tool_registry.names())

            # Trivially-passing case: both empty.
            if declared_all == set() and registered == set():
                continue

            # Drift invariant (post-optional_tool_names refactor):
            #   1. Every required tool MUST register. Missing-required is
            #      a real drift bug.
            #   2. Every registered tool MUST be declared (required OR
            #      optional). Surprise registrations are also drift.
            #   3. Optional tools are tolerated as missing (e.g.
            #      coding-harness's introspection tools when ``mss`` /
            #      ``rapidocr_onnxruntime`` aren't installed).
            missing_required = declared_required - registered
            surprise_registered = registered - declared_all
            if missing_required or surprise_registered:
                mismatches.append(
                    f"plugin {cand.manifest.id!r}: "
                    f"missing_required={sorted(missing_required)} "
                    f"surprise_registered={sorted(surprise_registered)} "
                    f"(declared_required={sorted(declared_required)} "
                    f"declared_optional={sorted(declared_optional)} "
                    f"registered={sorted(registered)})"
                )
    finally:
        # Restore sys.path + drop any synthetic plugin modules we loaded so
        # later tests see a clean slate.
        sys.path[:] = sys_path_snapshot
        for mod_name in set(sys.modules) - sys_modules_snapshot:
            sys.modules.pop(mod_name, None)

    assert not mismatches, (
        "tool_names drift detected in bundled plugin manifests:\n  "
        + "\n  ".join(mismatches)
    )
