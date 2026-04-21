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
    if "id" not in data or "name" not in data or "version" not in data:
        logger.warning("manifest %s missing required fields (id, name, version)", manifest_path)
        return None
    return PluginManifest(
        id=str(data["id"]),
        name=str(data["name"]),
        version=str(data["version"]),
        description=str(data.get("description", "")),
        author=str(data.get("author", "")),
        homepage=str(data.get("homepage", "")),
        license=str(data.get("license", "MIT")),
        kind=data.get("kind", "mixed"),
        entry=str(data.get("entry", "")),
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
