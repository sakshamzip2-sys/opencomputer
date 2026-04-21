"""
Config persistence — load/save ~/.opencomputer/config.yaml.

Users can edit this file by hand or via `opencomputer config set key=value`.
Defaults from ModelConfig/LoopConfig/etc. apply if the file is missing
or if a given key isn't set.
"""

from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
    _home,
    default_config,
)


def config_file_path() -> Path:
    return _home() / "config.yaml"


# ─── load ────────────────────────────────────────────────────────


def _apply_overrides(base: Any, overrides: dict[str, Any]) -> Any:
    """Recursively apply a dict over a dataclass, returning a new dataclass."""
    if not is_dataclass(base) or not isinstance(overrides, dict):
        return base
    field_map = {f.name: f for f in fields(base)}
    kwargs: dict[str, Any] = {}
    for name, current in asdict(base).items():
        if name in overrides:
            new = overrides[name]
            # Recurse into nested dataclasses
            nested = getattr(base, name)
            if is_dataclass(nested) and isinstance(new, dict):
                kwargs[name] = _apply_overrides(nested, new)
            else:
                # Cast Path strings back to Path
                field_type = field_map[name].type
                if "Path" in str(field_type) and isinstance(new, str):
                    kwargs[name] = Path(new)
                else:
                    kwargs[name] = new
        else:
            kwargs[name] = getattr(base, name)
    return type(base)(**kwargs)


def load_config(path: Path | None = None) -> Config:
    """Load config from YAML, applying overrides on top of defaults.

    Missing file or empty file → returns defaults. Invalid YAML is an error.
    """
    cfg_path = path or config_file_path()
    base = default_config()
    if not cfg_path.exists():
        return base
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise RuntimeError(f"Failed to parse {cfg_path}: {e}") from e
    if not isinstance(raw, dict):
        raise RuntimeError(f"Config file {cfg_path} must be a YAML mapping")
    return _apply_overrides(base, raw)


# ─── save ────────────────────────────────────────────────────────


def _to_yaml_dict(cfg: Config) -> dict[str, Any]:
    """Convert a Config dataclass to a YAML-friendly dict (Paths as strings)."""

    def _encode(v: Any) -> Any:
        if isinstance(v, Path):
            return str(v)
        if is_dataclass(v):
            return {k: _encode(getattr(v, k)) for k in [f.name for f in fields(v)]}
        return v

    return {
        "model": _encode(cfg.model),
        "loop": _encode(cfg.loop),
        "session": _encode(cfg.session),
        "memory": _encode(cfg.memory),
    }


def save_config(cfg: Config, path: Path | None = None) -> Path:
    """Write config to YAML. Returns the path written."""
    cfg_path = path or config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = _to_yaml_dict(cfg)
    cfg_path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return cfg_path


# ─── get / set by dotted key ────────────────────────────────────


def get_value(cfg: Config, key: str) -> Any:
    """Get a value by dotted key like 'model.provider'."""
    parts = key.split(".")
    current: Any = cfg
    for p in parts:
        if is_dataclass(current):
            if not hasattr(current, p):
                raise KeyError(f"Unknown config key: {key} (failed at '{p}')")
            current = getattr(current, p)
        else:
            raise KeyError(f"Unknown config key: {key} (not a config section at '{p}')")
    return current


def set_value(cfg: Config, key: str, value: Any) -> Config:
    """Return a NEW Config with `key` set to `value`. Dotted key supported."""
    parts = key.split(".")
    if len(parts) == 1:
        raise KeyError(f"Top-level set not supported: use e.g. 'model.provider'")
    section_name, *rest = parts
    if not hasattr(cfg, section_name):
        raise KeyError(f"Unknown section: {section_name}")

    section = getattr(cfg, section_name)
    if not is_dataclass(section):
        raise KeyError(f"'{section_name}' is not a config section")

    # Descend into nested sections (rare in this flat schema but future-proof)
    section_overrides: dict[str, Any] = {}
    cursor = section_overrides
    for i, p in enumerate(rest):
        if i == len(rest) - 1:
            cursor[p] = value
        else:
            cursor[p] = {}
            cursor = cursor[p]

    new_section = _apply_overrides(section, section_overrides)
    kwargs = {f.name: getattr(cfg, f.name) for f in fields(cfg)}
    kwargs[section_name] = new_section
    return Config(**kwargs)


__all__ = [
    "config_file_path",
    "load_config",
    "save_config",
    "get_value",
    "set_value",
]
