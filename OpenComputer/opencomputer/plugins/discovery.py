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
from dataclasses import dataclass
from pathlib import Path

from plugin_sdk.core import PluginManifest

logger = logging.getLogger("opencomputer.plugins.discovery")

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
    )


def discover(search_paths: list[Path]) -> list[PluginCandidate]:
    """
    Scan each path for `plugin.json` files. Return a list of PluginCandidates.

    Only direct children of each search path are considered (we don't recurse
    deeply — plugins live at `<root>/<plugin-id>/plugin.json`).
    """
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

    return candidates


__all__ = ["discover", "PluginCandidate"]
