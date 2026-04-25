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
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opencomputer.plugins.security import _path_is_inside, validate_plugin_root
from plugin_sdk.core import ModelSupport, PluginManifest

# I.8 — derivation provenance for a plugin's resolved id.
#
# Today the id only comes from the ``id`` field in ``plugin.json``
# (``"manifest"``). OpenClaw supports two more fallbacks —
# ``package.name`` from a package.json sibling and the directory
# basename (sources/openclaw/src/plugins/discovery.ts:678-725,
# ``resolvePackageExtensionEntries`` + ``deriveIdHint``). We keep those
# two values in the Literal so the field is first-class the day
# OpenComputer grows those paths; collision warnings already know how
# to surface whichever source produced each side.
IdSource = Literal["manifest", "package_name", "directory"]

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
    # I.8 — which derivation path supplied this candidate's id. Today
    # always ``"manifest"`` (id came from plugin.json); ``"package_name"``
    # and ``"directory"`` are reserved for future fallbacks so collision
    # logs can say exactly what each side resolved from.
    id_source: IdSource = "manifest"


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
    # Sub-project G.21 — flatten ModelSupportSchema → ModelSupport. None
    # means "no model affinity declared"; an empty ModelSupport (both
    # tuples empty) would also be treated as no-affinity by the matcher
    # but None keeps the wire shape honest about the manifest's intent.
    model_support = (
        ModelSupport(
            model_prefixes=tuple(schema.model_support.model_prefixes),
            model_patterns=tuple(schema.model_support.model_patterns),
        )
        if schema.model_support is not None
        else None
    )
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
        # Sub-project G.11 Tier 2.13 — MCP catalog binding
        mcp_servers=tuple(schema.mcp_servers),
        # Sub-project G.21 (Tier 4 OpenClaw port) — model-prefix auto-activation
        model_support=model_support,
    )


def _bundled_extensions_root() -> Path:
    """Resolved path to the repo's ``extensions/`` dir (I.1).

    ``validate_plugin_root`` loosens its permission/UID checks for
    plugins under this tree because some package managers widen
    bundled dirs during install. Matches ``standard_search_paths``'s
    derivation (``<__file__>.parent.parent.parent / "extensions"``).
    """
    return (Path(__file__).resolve().parent.parent.parent / "extensions").resolve()


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
    # Map id → already-accepted candidate so the collision warning can
    # name both sides' derivation paths and filesystem locations (I.8).
    seen: dict[str, PluginCandidate] = {}

    # I.1 — pre-resolve the bundled root so every candidate pays the
    # cost once per discover() call (and we don't re-import Path inside
    # the hot loop).
    bundled_root = _bundled_extensions_root()

    for root in search_paths:
        if not root.exists() or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name in _IGNORE_DIRS or entry.name.startswith("."):
                continue
            manifest_path = entry / "plugin.json"
            if not manifest_path.exists():
                continue
            # I.1 — filesystem security gate. Reject candidates with
            # symlink escapes / bad permissions / suspicious ownership
            # BEFORE parsing their manifest. Bundled plugins (under
            # ``extensions/``) get relaxed rules; user-installed plugins
            # fail closed. See opencomputer.plugins.security.
            try:
                is_bundled = _path_is_inside(entry.resolve(), bundled_root)
            except OSError:
                is_bundled = False
            check = validate_plugin_root(entry, root, is_bundled=is_bundled)
            if not check.ok:
                logger.warning(
                    "blocked plugin candidate at %s — %s",
                    entry,
                    check.reason,
                )
                continue
            manifest = _parse_manifest(manifest_path)
            if manifest is None:
                continue
            # Today every candidate's id comes from plugin.json's ``id``
            # field. The ``id_source`` field is future-proofed for
            # package_name / directory fallbacks; once those land, this
            # literal is where the derivation path gets decided.
            new_candidate = PluginCandidate(
                manifest=manifest,
                root_dir=entry,
                manifest_path=manifest_path,
                id_source="manifest",
            )
            existing = seen.get(manifest.id)
            if existing is not None:
                logger.warning(
                    "plugin id collision: '%s' (%s at %s, %s at %s) — "
                    "skipping second occurrence at %s",
                    manifest.id,
                    existing.id_source,
                    existing.root_dir,
                    new_candidate.id_source,
                    new_candidate.root_dir,
                    entry,
                )
                continue
            seen[manifest.id] = new_candidate
            candidates.append(new_candidate)

    # Store an independent list so later cache hits can hand out a fresh
    # copy without the canonical entry being affected by caller mutations.
    _discovery_cache[key] = (now, list(candidates))
    return candidates


def find_plugin_ids_for_model(
    model_id: str,
    candidates: list[PluginCandidate],
) -> list[str]:
    """Return ids of plugins whose ``model_support`` matches ``model_id``.

    Sub-project G.21 (Tier 4 OpenClaw port). Pure function — no
    filesystem I/O, no plugin loading. Used by the registry to expand
    ``enabled_ids`` so a user who picks ``gpt-4o`` automatically gets
    ``openai-provider`` activated even if their profile preset didn't
    list it.

    Match order mirrors OpenClaw
    (``sources/openclaw-2026.4.23/src/plugins/providers.ts:316-337``):

    1. ``model_patterns`` first — regex via ``re.search`` (any match
       anywhere in the id wins). Bad patterns are silently skipped so
       one malformed manifest doesn't break the rest of the registry.
    2. ``model_prefixes`` second — ``str.startswith``.

    Empty ``model_id`` returns ``[]`` (the "user hasn't picked a model
    yet" path — fresh installs go through the setup wizard, not this
    helper). Result is sorted alphabetically for deterministic ordering;
    matches OpenClaw's ``dedupeSortedPluginIds``.
    """
    if not model_id:
        return []
    matches: set[str] = set()
    for cand in candidates:
        ms = cand.manifest.model_support
        if ms is None:
            continue
        matched = False
        for pattern in ms.model_patterns:
            try:
                if re.search(pattern, model_id):
                    matched = True
                    break
            except re.error:
                logger.warning(
                    "plugin %s declares invalid model_pattern %r — skipping",
                    cand.manifest.id,
                    pattern,
                )
                continue
        if not matched:
            for prefix in ms.model_prefixes:
                if model_id.startswith(prefix):
                    matched = True
                    break
        if matched:
            matches.add(cand.manifest.id)
    return sorted(matches)


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


__all__ = [
    "discover",
    "find_plugin_ids_for_model",
    "IdSource",
    "PluginCandidate",
    "standard_search_paths",
]
