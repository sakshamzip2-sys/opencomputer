"""Proxy-file persistence + recursive path rewriting.

When a remote node-target generates files (screenshots / downloads) the
wire payload carries them base64-encoded. We:

  1. Decode + write to the local profile media store.
  2. Walk the result tree and rewrite any ``path`` / ``imagePath`` /
     ``download.path`` / ``filePath`` field that points at the remote
     name so the agent sees a real local path it can ``Read``.

Fixes OpenClaw's bug: the TS source only inspected three known fields at
depth 1 and silently dropped paths in nested structures (e.g. an array
of download records). The Python port walks recursively.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from .._utils.atomic_write import atomic_write_bytes

#: Field names that hold a remote path that may need rewriting. Kept
#: explicit (vs "any string field") so we don't accidentally rewrite
#: arbitrary user data that happens to look like a path.
_PATH_FIELDS: frozenset[str] = frozenset(
    {"path", "imagePath", "image_path", "filePath", "file_path", "downloadPath", "download_path"}
)


async def persist_proxy_files(
    files: list[dict[str, Any]] | None,
    *,
    media_root: Path,
) -> dict[str, str]:
    """Decode each ``{path, base64, mimeType?}`` record into local media.

    Returns ``{remote_path: local_path}`` map for use with
    :func:`apply_proxy_paths`.
    """
    if not files:
        return {}
    media_root.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        remote_path = entry.get("path")
        b64 = entry.get("base64") or entry.get("data")
        if not isinstance(remote_path, str) or not isinstance(b64, str) or not remote_path:
            continue
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception:
            continue
        local_name = os.path.basename(remote_path) or "proxy-file"
        local_path = media_root / local_name
        # Disambiguate name collisions
        if local_path.exists():
            stem = local_path.stem
            suffix = local_path.suffix
            counter = 1
            while local_path.exists():
                local_path = media_root / f"{stem}-{counter}{suffix}"
                counter += 1
        atomic_write_bytes(local_path, data)
        mapping[remote_path] = str(local_path)
    return mapping


def apply_proxy_paths(result: Any, mapping: dict[str, str]) -> None:
    """Recursively walk ``result`` and rewrite any path field via ``mapping``.

    Mutates ``result`` in place. Paths not in ``mapping`` are left
    untouched. Cycles are tolerated (shallow visited-set guard).
    """
    if not mapping:
        return
    _walk(result, mapping, set())


def _walk(node: Any, mapping: dict[str, str], visited: set[int]) -> None:
    if isinstance(node, dict):
        if id(node) in visited:
            return
        visited.add(id(node))
        for k, v in list(node.items()):
            if k in _PATH_FIELDS and isinstance(v, str) and v in mapping:
                node[k] = mapping[v]
            else:
                _walk(v, mapping, visited)
    elif isinstance(node, list):
        if id(node) in visited:
            return
        visited.add(id(node))
        for item in node:
            _walk(item, mapping, visited)
    # Other scalars: nothing to do.


__all__ = ["apply_proxy_paths", "persist_proxy_files"]
