"""Tests for I.3 — lazy-reflection invariants on the plugin registry.

OpenClaw's loader uses lazy-reflection getters so registry introspection
never forces a full module import (sources/openclaw/src/plugins/loader.ts
around ``LAZY_RUNTIME_REFLECTION_KEYS``). OpenComputer's equivalent is
the two-phase discover-then-load pattern:

  1. ``discovery.discover()`` walks ``plugin.json`` files — pure JSON I/O,
     zero imports of plugin entry modules.
  2. ``loader.load_plugin()`` imports the entry module + runs
     ``register(api)`` — only called when a plugin is actually activated.

This file enforces that invariant so we don't accidentally regress it
(e.g. by importing an entry module inside ``_parse_manifest`` for a
"convenience" computed field, which would make ``opencomputer plugins``
pay activation cost on every run).

I.3 finding: the two-phase model was already correct. No refactor was
required — only these tests to prevent regression. No ``@cached_property``
was added because ``PluginManifest`` / ``PluginCandidate`` currently
have no derived fields beyond the parsed manifest itself (both are
``frozen=True, slots=True`` so the moment someone adds a derived
accessor, that's the place to apply ``@cached_property``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import PluginCandidate, discover


def _write_plugin(
    root: Path,
    plugin_id: str,
    entry: str = "plugin",
    entry_body: str | None = None,
) -> Path:
    """Scaffold a minimal valid plugin.json + entry module under ``root``.

    ``entry_body`` lets a test inject a poison-pill entry module that
    RAISES on import. If discovery accidentally imports the entry, the
    test crashes loudly with the raise — far better than a silent perf
    regression slipping through CI.
    """
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").title(),
        "version": "0.0.1",
        "kind": "tool",
        "entry": entry,
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    if entry_body is None:
        entry_body = (
            "# poison-pill entry. Any code path that *imports* this module "
            "during pure discovery has broken the two-phase invariant.\n"
            "raise RuntimeError("
            "'plugin entry module was imported during discovery — this "
            "violates the two-phase lazy-reflection invariant')\n"
        )
    (plugin_dir / f"{entry}.py").write_text(entry_body, encoding="utf-8")
    return plugin_dir


def _plugin_module_keys(modules: Iterable[str]) -> set[str]:
    """Filter ``sys.modules`` keys for anything that looks plugin-entry-ish.

    Anything matching:
      - the synthetic loader prefix ``_opencomputer_plugin_``
      - bare common entry names (``plugin``, ``provider``, ``adapter``,
        ``handlers``, ``hooks``) that plugin-loader clears between loads

    Anything else (pydantic/httpx/test scaffolding) is irrelevant for
    this invariant and is filtered out.
    """
    suspicious = {"plugin", "provider", "adapter", "handlers", "hooks"}
    out: set[str] = set()
    for name in modules:
        if name.startswith("_opencomputer_plugin_"):
            out.add(name)
            continue
        # Bare module names only — pydantic's ``pydantic.fields`` is not
        # a plugin import. ``plugin`` alone is.
        if name in suspicious:
            out.add(name)
    return out


@pytest.fixture(autouse=True)
def _clear_cache_between_tests() -> None:
    """Every test starts with a fresh discovery cache."""
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


@pytest.fixture
def plugin_root(tmp_path: Path) -> Path:
    """Three plugins whose entry modules RAISE on import.

    If any code path accidentally imports an entry module during
    discovery or metadata access, the import-time ``raise`` will
    propagate out of the test and fail loudly.
    """
    root = tmp_path / "plugins"
    root.mkdir()
    _write_plugin(root, "alpha")
    _write_plugin(root, "beta")
    _write_plugin(root, "gamma")
    return root


# ─── core invariant ─────────────────────────────────────────────────────


def test_discover_does_not_import_entry_modules(plugin_root: Path) -> None:
    """``discover()`` must walk N plugin dirs without importing any entry.

    Two safety nets:
      1. Each plugin's entry module ``raise``s at import time — so
         silent imports become loud failures.
      2. Snapshot ``sys.modules`` before + after and diff; no new
         plugin-entry-ish modules may appear.
    """
    before = _plugin_module_keys(sys.modules.keys())

    candidates = discover([plugin_root])

    after = _plugin_module_keys(sys.modules.keys())

    # All three plugins discovered — pure JSON walk.
    assert {c.manifest.id for c in candidates} == {"alpha", "beta", "gamma"}
    # And no entry module snuck into sys.modules.
    added = after - before
    assert added == set(), (
        f"discover() imported entry modules it shouldn't have: {added!r}"
    )


def test_manifest_fields_readable_without_imports(plugin_root: Path) -> None:
    """Every ``PluginManifest`` field must be reachable post-discovery with no imports.

    This is the "listing UI" path: ``opencomputer plugins`` iterates
    candidates and reads id / version / description / kind / entry /
    profiles / single_instance / enabled_by_default / tool_names.
    None of those reads may trigger an import.
    """
    before = _plugin_module_keys(sys.modules.keys())

    candidates = discover([plugin_root])
    # Touch every manifest field + the PluginCandidate's own fields.
    rendered: list[str] = []
    for c in candidates:
        m = c.manifest
        rendered.append(
            f"{m.id}|{m.name}|{m.version}|{m.description}|{m.author}|"
            f"{m.homepage}|{m.license}|{m.kind}|{m.entry}|{m.profiles}|"
            f"{m.single_instance}|{m.enabled_by_default}|{m.tool_names}"
        )
        # Candidate-level fields too.
        assert c.root_dir.is_dir()
        assert c.manifest_path.name == "plugin.json"
        assert c.id_source == "manifest"

    assert len(rendered) == 3

    after = _plugin_module_keys(sys.modules.keys())
    assert (after - before) == set(), (
        "reading manifest fields triggered a plugin-entry import"
    )


def test_list_candidates_on_registry_is_lazy(plugin_root: Path) -> None:
    """``PluginRegistry.list_candidates`` — the method ``opencomputer plugins`` uses."""
    from opencomputer.plugins.registry import PluginRegistry

    before = _plugin_module_keys(sys.modules.keys())

    reg = PluginRegistry()
    candidates = reg.list_candidates([plugin_root])

    after = _plugin_module_keys(sys.modules.keys())

    assert len(candidates) == 3
    assert all(isinstance(c, PluginCandidate) for c in candidates)
    # And the registry itself never activated anything.
    assert reg.loaded == []
    assert (after - before) == set()


def test_plugins_cli_does_not_import_entry_modules(
    plugin_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``opencomputer plugins`` must list without importing any plugin entry.

    Invokes the Typer command through ``CliRunner`` and verifies:
      - exit code is zero
      - stdout contains every plugin id
      - no entry modules were imported as a side effect
    """
    from typer.testing import CliRunner

    from opencomputer.cli import app

    # Point ``standard_search_paths`` at our poison-pill plugin root so
    # the CLI uses that instead of the real ``extensions/`` tree.
    def _fake_paths() -> list[Path]:
        return [plugin_root]

    # Patch both the definition site and the call site (cli.py does
    # ``from opencomputer.plugins.discovery import standard_search_paths``
    # inline inside the ``plugins`` function — that creates a local
    # binding that outlives any module-level patch, so we just patch
    # the source module which ``plugins()`` freshly imports on each call).
    monkeypatch.setattr(
        "opencomputer.plugins.discovery.standard_search_paths", _fake_paths
    )

    before = _plugin_module_keys(sys.modules.keys())

    runner = CliRunner()
    result = runner.invoke(app, ["plugins"])

    after = _plugin_module_keys(sys.modules.keys())

    assert result.exit_code == 0, (
        f"plugins CLI exit_code={result.exit_code}, "
        f"stdout={result.stdout!r}, exception={result.exception!r}"
    )
    # Every plugin id rendered in the listing.
    for plugin_id in ("alpha", "beta", "gamma"):
        assert plugin_id in result.stdout, (
            f"plugins CLI did not print {plugin_id!r} — got {result.stdout!r}"
        )
    # And no entry module imports happened as a side-effect.
    added = after - before
    assert added == set(), (
        f"plugins CLI triggered plugin-entry imports: {added!r}"
    )


# ─── positive control — the two-phase pattern DOES import on load ──────


def test_load_plugin_is_what_actually_imports(tmp_path: Path) -> None:
    """Sanity check: the two-phase split is real — ``load_plugin`` DOES import.

    If this test regresses (``load_plugin`` stops importing) we've broken
    the loader, not the laziness invariant. But it keeps the contract
    honest: discovery is cheap, loading is where import cost lives.
    """
    from opencomputer.plugins.loader import PluginAPI, load_plugin

    root = tmp_path / "plugins"
    root.mkdir()
    _write_plugin(
        root,
        "loader-positive",
        entry_body=(
            "CALLED = []\n"
            "def register(api):\n"
            "    CALLED.append('registered')\n"
        ),
    )

    before = _plugin_module_keys(sys.modules.keys())

    candidates = discover([root])
    assert len(candidates) == 1
    # Discovery alone imports nothing.
    assert _plugin_module_keys(sys.modules.keys()) - before == set()

    # Loading imports exactly one synthetic plugin module.
    class _Noop:
        def __init__(self) -> None:
            self._items: list[object] = []

        def names(self) -> list[str]:
            return []

        def register(self, *args, **kwargs) -> None:
            self._items.append((args, kwargs))

    api = PluginAPI(
        tool_registry=_Noop(),
        hook_engine=_Noop(),
        provider_registry={},
        channel_registry={},
    )
    loaded = load_plugin(candidates[0], api)
    assert loaded is not None

    after = _plugin_module_keys(sys.modules.keys())
    new = after - before
    # At least the synthetic module should now be present — proving the
    # import only happened at load time, never at discover time.
    assert any(name.startswith("_opencomputer_plugin_") for name in new), (
        f"load_plugin didn't import a synthetic plugin module "
        f"(got new modules: {new!r})"
    )
