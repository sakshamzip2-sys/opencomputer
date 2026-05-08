"""Shared profile.yaml + config.yaml writers (Wave 6.D-α).

Extracted from ``opencomputer/cli_plugin.py`` so the dashboard mutation
endpoints (Wave 6.D-α) and the existing CLI helpers share one crash-
safe writer. The original lived in cli_plugin.py since v0.1; this
module is the new authoritative location, and cli_plugin.py re-exports
for backward compatibility.

Crash-safety guarantees:
- Atomic write via tmp file + ``os.replace``. ``os.replace`` is
  atomic on POSIX and same-volume on Windows.
- For read-modify-write cycles, :func:`modify_yaml_locked` wraps
  :func:`opencomputer.profiles_lock.profile_yaml_lock` (PR #431) so
  concurrent dashboard mutations and CLI ``oc plugin enable/disable``
  invocations from sibling shells serialize cleanly at the directory
  level instead of last-write-wins'ing each other.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "atomic_write_yaml",
    "modify_yaml_locked",
    "load_yaml",
    "set_default_personality",
    "set_display_skin",
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

    Wraps :func:`opencomputer.profiles_lock.profile_yaml_lock` so
    concurrent callers (dashboard + CLI) serialize at the directory
    level. ``mutate`` is called with the parsed dict and must mutate
    it in place. Returns the new dict for the caller's convenience.

    Closes the read-modify-write race against PR #431's CLI flock —
    dashboard mutations now interlock with ``oc plugin enable`` from
    a sibling shell.
    """
    from opencomputer.profiles_lock import profile_yaml_lock

    path.parent.mkdir(parents=True, exist_ok=True)
    with profile_yaml_lock(path.parent):
        data = load_yaml(path)
        mutate(data)
        atomic_write_yaml(path, data)
    return data


def set_default_personality(config_path: Path, name: str) -> None:
    """Persist ``agent.default_personality: name`` to the profile config.

    Empty ``name`` removes the key (so resolution falls back to the
    built-in 'helpful'). Uses :func:`modify_yaml_locked` so concurrent
    writers serialize cleanly.
    """
    def _mutate(data: dict[str, Any]) -> None:
        agent = data.setdefault("agent", {})
        if not isinstance(agent, dict):
            agent = {}
            data["agent"] = agent
        if name:
            agent["default_personality"] = name
        else:
            agent.pop("default_personality", None)

    modify_yaml_locked(config_path, _mutate)


def set_display_skin(config_path: Path, name: str) -> None:
    """Persist ``display.skin: name`` to the profile config.

    Empty ``name`` removes the key (so resolution falls back to the
    built-in 'default' skin). Uses :func:`modify_yaml_locked` so
    concurrent writers serialize cleanly.
    """
    def _mutate(data: dict[str, Any]) -> None:
        display = data.setdefault("display", {})
        if not isinstance(display, dict):
            display = {}
            data["display"] = display
        if name:
            display["skin"] = name
        else:
            display.pop("skin", None)

    modify_yaml_locked(config_path, _mutate)
