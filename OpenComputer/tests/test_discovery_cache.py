"""Tests for the TTL cache in `opencomputer.plugins.discovery` (I.2).

The cache collapses bursty plugin-discovery rescans to a single filesystem
walk per 1-second window, keyed on ``(search_paths, uid)``. Matches
OpenClaw's pattern (sources/openclaw/src/plugins/discovery.ts:61-91).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from opencomputer.plugins import discovery
from opencomputer.plugins.discovery import (
    _DISCOVERY_TTL_SEC,
    PluginCandidate,
    discover,
)


def _write_manifest(root: Path, plugin_id: str, entry: str = "plugin") -> Path:
    """Scaffold a minimal valid plugin.json under ``root / plugin_id``.

    ``entry`` is a Python module name (no ``.py`` suffix) per the
    manifest schema. We also drop a matching ``<entry>.py`` so callers
    that load the plugin don't trip on a missing file.
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
    manifest_path = plugin_dir / "plugin.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / f"{entry}.py").write_text("", encoding="utf-8")
    return plugin_dir


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Each test starts with an empty discovery cache."""
    discovery._discovery_cache.clear()
    yield
    discovery._discovery_cache.clear()


@pytest.fixture
def plugins_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    root.mkdir()
    _write_manifest(root, "alpha")
    _write_manifest(root, "beta")
    return root


def _set_time(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    """Pin ``time.monotonic`` inside the discovery module to ``value``."""
    monkeypatch.setattr(discovery.time, "monotonic", lambda: value)


def test_first_call_populates_cache(
    monkeypatch: pytest.MonkeyPatch, plugins_root: Path
) -> None:
    _set_time(monkeypatch, 100.0)
    assert discovery._discovery_cache == {}

    result = discover([plugins_root])

    assert len(result) == 2
    assert all(isinstance(c, PluginCandidate) for c in result)
    assert len(discovery._discovery_cache) == 1


def test_second_call_within_ttl_returns_cached(
    monkeypatch: pytest.MonkeyPatch, plugins_root: Path
) -> None:
    _set_time(monkeypatch, 100.0)
    first = discover([plugins_root])

    # Stale the filesystem — delete a plugin dir so that a rescan would
    # produce a DIFFERENT result than the cached one.
    shutil.rmtree(plugins_root / "alpha")

    # Advance time but stay strictly within the TTL window.
    _set_time(monkeypatch, 100.0 + _DISCOVERY_TTL_SEC - 0.1)
    second = discover([plugins_root])

    assert [c.manifest.id for c in second] == [c.manifest.id for c in first]
    assert len(second) == 2  # would be 1 if rescan had happened


def test_second_call_after_ttl_rescans(
    monkeypatch: pytest.MonkeyPatch, plugins_root: Path
) -> None:
    _set_time(monkeypatch, 100.0)
    first = discover([plugins_root])
    assert len(first) == 2

    # Remove a plugin after the first discovery.
    shutil.rmtree(plugins_root / "alpha")

    # Advance past the TTL — next call MUST rescan.
    _set_time(monkeypatch, 100.0 + _DISCOVERY_TTL_SEC + 0.01)
    second = discover([plugins_root])

    assert len(second) == 1
    assert second[0].manifest.id == "beta"


def test_force_rescan_bypasses_cache(
    monkeypatch: pytest.MonkeyPatch, plugins_root: Path
) -> None:
    _set_time(monkeypatch, 100.0)
    first = discover([plugins_root])
    assert len(first) == 2

    # Remove a plugin — cache still holds the 2-entry result.
    shutil.rmtree(plugins_root / "alpha")

    # Still inside TTL, but force_rescan must bypass and refresh.
    _set_time(monkeypatch, 100.0 + 0.1)
    fresh = discover([plugins_root], force_rescan=True)

    assert len(fresh) == 1
    assert fresh[0].manifest.id == "beta"

    # And a subsequent non-forced call within the new TTL should see the
    # refreshed entry, not the stale two-plugin result.
    _set_time(monkeypatch, 100.0 + 0.2)
    third = discover([plugins_root])
    assert len(third) == 1


def test_different_search_paths_keys_separate_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _write_manifest(root_a, "a-only")
    _write_manifest(root_b, "b-only")

    _set_time(monkeypatch, 100.0)
    result_a = discover([root_a])
    result_b = discover([root_b])

    assert [c.manifest.id for c in result_a] == ["a-only"]
    assert [c.manifest.id for c in result_b] == ["b-only"]
    assert len(discovery._discovery_cache) == 2


def test_returned_list_is_fresh_copy(
    monkeypatch: pytest.MonkeyPatch, plugins_root: Path
) -> None:
    _set_time(monkeypatch, 100.0)
    first = discover([plugins_root])
    assert len(first) == 2

    # Mutate the caller's list — if the cache is handing out its internal
    # list by reference, the next call will see the mutation.
    first.clear()
    first.append("not a candidate")  # type: ignore[arg-type]

    # Still inside TTL — must still return the original two candidates.
    _set_time(monkeypatch, 100.0 + 0.1)
    second = discover([plugins_root])

    assert len(second) == 2
    assert all(isinstance(c, PluginCandidate) for c in second)


def test_cache_key_includes_uid(
    monkeypatch: pytest.MonkeyPatch, plugins_root: Path
) -> None:
    """The cache key embeds the effective UID so separate users don't alias."""
    _set_time(monkeypatch, 100.0)

    # First discovery under one uid.
    monkeypatch.setattr(discovery.os, "geteuid", lambda: 1000, raising=False)
    discover([plugins_root])

    # Second discovery with a different effective uid — even within TTL the
    # cache should see it as a miss and store a separate entry.
    monkeypatch.setattr(discovery.os, "geteuid", lambda: 2000, raising=False)
    discover([plugins_root])

    assert len(discovery._discovery_cache) == 2
