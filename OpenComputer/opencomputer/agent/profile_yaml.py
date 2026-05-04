"""Shared profile.yaml + config.yaml writers (Wave 6.D-α).

Extracted from ``opencomputer/cli_plugin.py`` so the dashboard mutation
endpoints (PR Wave 6.D-α) and the existing CLI helpers share one
crash-safe writer. The original lived in cli_plugin.py since v0.1; this
module is the new authoritative location, and cli_plugin.py re-exports
for backward compatibility.

Crash-safety guarantees:
- Atomic write via ``tempfile.NamedTemporaryFile`` + ``os.replace``.
  ``os.replace`` is atomic on POSIX and same-volume on Windows.
- ``filelock.FileLock`` ringfences the read-modify-write window so two
  concurrent dashboard mutations (or dashboard + CLI) can't lose
  updates. Lock file lives next to the target as ``<name>.lock``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from filelock import FileLock

__all__ = [
    "atomic_write_yaml",
    "modify_yaml_locked",
    "load_yaml",
]


def load_yaml(path: Path) -> dict[str, Any]:
    """Read ``path`` as YAML mapping. Missing file → empty dict."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path} must contain a mapping at the top level (got {type(raw).__name__})"
        )
    return raw


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` as YAML via tmp + ``os.replace``.

    A partial write lands in ``<path>.tmp`` which is never visible to
    readers. ``os.replace`` is atomic on POSIX and on Windows for same-
    volume moves (always our case here).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    os.replace(tmp, path)


def modify_yaml_locked(path: Path, mutate) -> dict[str, Any]:
    """Read ``path``, apply ``mutate(data)``, atomically write back.

    Held under a ``filelock.FileLock`` so concurrent callers (dashboard
    + CLI) serialize at the file level. ``mutate`` is called with the
    parsed dict and must mutate it in place (returning anything is OK
    but ignored). Returns the new dict for the caller's convenience.

    Audit lens A3: closes the read-modify-write race that two browser
    tabs or a dashboard + CLI race would otherwise hit.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path), timeout=10):
        data = load_yaml(path)
        mutate(data)
        atomic_write_yaml(path, data)
    return data
