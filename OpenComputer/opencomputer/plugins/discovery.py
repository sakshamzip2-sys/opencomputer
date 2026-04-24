"""
Plugin discovery — Phase 1 of the two-phase loader.

Walk the extensions/ and ~/.opencomputer/plugins/ directories, find
`plugin.json` manifests, and build PluginCandidates. This phase is
CHEAP — only JSON reads, no imports.

Phase 2 (loader.py) activates a candidate on demand by importing its
entry module and letting it register its tools/channels/hooks.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from plugin_sdk.core import PluginManifest

logger = logging.getLogger("opencomputer.plugins.discovery")

# I.2 — TTL cache for plugin discovery. Bursty CLI flows (e.g. multiple
# `opencomputer plugins` calls, doctor + CLI in sequence, tests) would
# otherwise pay filesystem I/O on every call. The window is deliberately
# short (1 s): long enough to collapse same-tick rescans, short enough
# that a freshly-installed plugin shows up within normal human latency.
#
# Keyed on ``tuple(search_paths) + (uid,)`` so concurrent processes /
# profiles / test setups with different roots don't alias. In-memory only
# — the cache never persists across process restarts, which matches
# OpenClaw's ``discoveryCache`` (sources/openclaw/src/plugins/
# discovery.ts:61-91, ``getCachedDiscoveryResult``).
_discovery_cache: dict[tuple, tuple[float, list[PluginCandidate]]] = {}
_DISCOVERY_TTL_SEC = 1.0

_IGNORE_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    """Metadata-only view of an installed plugin — output of discovery."""

    manifest: PluginManifest
    root_dir: Path
    manifest_path: Path


def _parse_manifest(manifest_path: Path) -> PluginManifest | None:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to parse manifest %s: %s", manifest_path, e)
        return None
    # Phase 12g: typed pydantic validation runs first so wrong types,
    # unknown kinds, malformed ids etc. fail with a useful message before
    # we ever construct the dataclass. One bad plugin shouldn't break
    # the rest — log + return None.
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest(data)
    if schema is None:
        logger.warning("invalid manifest %s — %s", manifest_path, err)
        return None
    return PluginManifest(
        id=schema.id,
        name=schema.name,
        version=schema.version,
        description=schema.description,
        author=schema.author,
        homepage=schema.homepage,
        license=schema.license,
        kind=schema.kind,
        entry=schema.entry,
        # Phase 14.C — profile scoping. ["*"] or None means "any profile".
        profiles=(tuple(schema.profiles) if schema.profiles is not None else None),
        single_instance=schema.single_instance,
        # Phase 12b1 Sub-project A — Honcho-as-default
        enabled_by_default=schema.enabled_by_default,
        # Phase 12b5 Sub-project E — tool_names for demand-driven activation
        tool_names=tuple(schema.tool_names),
    )


def _cache_key(search_paths: list[Path]) -> tuple:
    """Cache key for ``discover`` — paths + effective uid.

    The uid guards against aliasing when multiple users share a host
    (``~/.opencomputer/plugins`` resolves differently per user but the
    raw path tuple could still match on e.g. `/tmp` fixtures). On
    platforms without ``os.geteuid`` (Windows) the uid component
    collapses to ``0``.
    """
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    return tuple(search_paths) + (uid,)


def discover(
    search_paths: list[Path],
    force_rescan: bool = False,
) -> list[PluginCandidate]:
    """
    Scan each path for `plugin.json` files. Return a list of PluginCandidates.

    Only direct children of each search path are considered (we don't recurse
    deeply — plugins live at `<root>/<plugin-id>/plugin.json`).

    Results are cached for ``_DISCOVERY_TTL_SEC`` keyed on the search paths
    and effective uid. Pass ``force_rescan=True`` to bypass the cache (the
    refreshed result still populates the cache for subsequent calls).
    """
    key = _cache_key(search_paths)
    now = time.monotonic()

    if not force_rescan:
        hit = _discovery_cache.get(key)
        if hit is not None:
            stored_at, cached = hit
            if now - stored_at < _DISCOVERY_TTL_SEC:
                # Return a shallow copy so callers mutating the list don't
                # corrupt the cache's internal state. PluginCandidate is
                # frozen so the elements themselves are safe to share.
                return list(cached)
            # Expired — drop it so the cache doesn't accumulate stale keys.
            _discovery_cache.pop(key, None)

    candidates: list[PluginCandidate] = []
    seen_ids: set[str] = set()

    for root in search_paths:
        if not root.exists() or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name in _IGNORE_DIRS or entry.name.startswith("."):
                continue
            manifest_path = entry / "plugin.json"
            if not manifest_path.exists():
                continue
            manifest = _parse_manifest(manifest_path)
            if manifest is None:
                continue
            if manifest.id in seen_ids:
                logger.warning(
                    "plugin id collision: '%s' — skipping second occurrence at %s",
                    manifest.id,
                    entry,
                )
                continue
            seen_ids.add(manifest.id)
            candidates.append(
                PluginCandidate(
                    manifest=manifest,
                    root_dir=entry,
                    manifest_path=manifest_path,
                )
            )

    # Store an independent list so later cache hits can hand out a fresh
    # copy without the canonical entry being affected by caller mutations.
    _discovery_cache[key] = (now, list(candidates))
    return candidates


def standard_search_paths() -> list[Path]:
    """Canonical plugin search-path list, in priority order.

    ``discover()`` dedupes by id, so higher-priority roots shadow
    lower-priority ones. Priority (highest first):

      1. Profile-local — ``<active_profile_dir>/plugins/``  (only
         present for named profiles; for the default profile the
         profile dir == default_root so this collapses into step 2).
      2. Global        — ``~/.opencomputer/plugins/``
      3. Bundled       — ``<repo>/extensions/``

    Non-existent directories are omitted. Does not swallow exceptions
    from profile/config resolution — callers that need silent failure
    wrap the call themselves (see ``AgentLoop._default_search_paths``).

    Single source of truth for the plugin search paths used by
    ``cli._discover_plugins``, ``cli.plugins`` (the listing command),
    ``cli_plugin.plugin_enable``, and ``AgentLoop._default_search_paths``.
    """
    # Lazy imports — avoid cycles with opencomputer.agent.config /
    # opencomputer.profiles, which are loaded later in the cli chain.
    from opencomputer.agent.config import _home
    from opencomputer.profiles import get_default_root, read_active_profile

    search_paths: list[Path] = []

    active = read_active_profile()
    default_root = get_default_root()
    profile_dir = _home()

    # 1. Profile-local (only distinct from global for named profiles)
    if active is not None:
        profile_local = profile_dir / "plugins"
        if profile_local.exists():
            search_paths.append(profile_local)

    # 2. Global
    global_plugins = default_root / "plugins"
    if global_plugins.exists() and global_plugins not in search_paths:
        search_paths.append(global_plugins)

    # 3. Bundled (extensions/) — __file__ is at
    # OpenComputer/opencomputer/plugins/discovery.py, so
    # parent.parent.parent resolves to the OpenComputer/ repo root.
    repo_root = Path(__file__).resolve().parent.parent.parent
    ext_dir = repo_root / "extensions"
    if ext_dir.exists():
        search_paths.append(ext_dir)

    return search_paths


__all__ = ["discover", "PluginCandidate", "standard_search_paths"]
