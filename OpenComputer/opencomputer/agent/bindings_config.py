"""Bindings schema for ~/.opencomputer/bindings.yaml.

Format
------
::
    default_profile: default
    bindings:
      - match: { platform: telegram, chat_id: "12345" }
        profile: coding
        priority: 100
      - match: { platform: telegram }
        profile: personal
        priority: 10

Match field semantics
---------------------
- All match fields are optional. Empty match = catch-all (matches every event).
- Match values are exact string. (Regex/glob deferred.)
- Multiple fields in one match are AND-ed.

Loaded by ``BindingResolver`` at gateway boot. Schema-strict: unknown
top-level keys raise. Frozen dataclasses prevent drive-by mutation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("opencomputer.agent.bindings_config")


@dataclass(frozen=True, slots=True)
class BindingMatch:
    """Optional match fields. Empty = catch-all (matches every event)."""

    platform: str | None = None
    chat_id: str | None = None
    group_id: str | None = None
    peer_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True, slots=True)
class Binding:
    """One routing rule: ``match`` predicate -> ``profile`` id, with priority."""

    match: BindingMatch
    profile: str
    priority: int = 0


@dataclass(frozen=True, slots=True)
class BindingsConfig:
    """Parsed contents of ``bindings.yaml``."""

    default_profile: str = "default"
    bindings: tuple[Binding, ...] = field(default_factory=tuple)


_ALLOWED_TOP_LEVEL: frozenset[str] = frozenset({"default_profile", "bindings"})
_ALLOWED_MATCH_KEYS: frozenset[str] = frozenset(
    {"platform", "chat_id", "group_id", "peer_id", "account_id"}
)


def load_bindings(path: Path) -> BindingsConfig:
    """Load ``bindings.yaml`` from disk. Missing file → defaults.

    Raises
    ------
    ValueError
        Malformed schema (wrong types, unknown fields).
    """
    if not path.exists():
        logger.debug("bindings: no file at %s — default-only routing", path)
        return BindingsConfig()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")

    unknown = set(raw.keys()) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ValueError(f"{path}: unknown top-level field(s): {sorted(unknown)}")

    default_profile = raw.get("default_profile", "default")
    if not isinstance(default_profile, str):
        raise ValueError(
            f"{path}: default_profile must be a string, got {type(default_profile).__name__}"
        )

    raw_bindings = raw.get("bindings", []) or []
    if not isinstance(raw_bindings, list):
        raise ValueError(f"{path}: bindings must be a list")

    bindings: list[Binding] = []
    for i, b in enumerate(raw_bindings):
        if not isinstance(b, dict):
            raise ValueError(f"{path}: bindings[{i}] must be a mapping")
        match_raw = b.get("match", {}) or {}
        if not isinstance(match_raw, dict):
            raise ValueError(f"{path}: bindings[{i}].match must be a mapping")
        unknown_match = set(match_raw.keys()) - _ALLOWED_MATCH_KEYS
        if unknown_match:
            raise ValueError(
                f"{path}: bindings[{i}].match unknown field(s): {sorted(unknown_match)}"
            )
        # Coerce match values to str (chat_id is often int in YAML).
        match = BindingMatch(
            platform=str(match_raw["platform"]) if "platform" in match_raw else None,
            chat_id=str(match_raw["chat_id"]) if "chat_id" in match_raw else None,
            group_id=str(match_raw["group_id"]) if "group_id" in match_raw else None,
            peer_id=str(match_raw["peer_id"]) if "peer_id" in match_raw else None,
            account_id=str(match_raw["account_id"]) if "account_id" in match_raw else None,
        )
        profile = b.get("profile")
        if not isinstance(profile, str) or not profile:
            raise ValueError(f"{path}: bindings[{i}].profile must be a non-empty string")
        priority_raw = b.get("priority", 0)
        if not isinstance(priority_raw, int):
            raise ValueError(f"{path}: bindings[{i}].priority must be an int")
        bindings.append(Binding(match=match, profile=profile, priority=priority_raw))

    return BindingsConfig(default_profile=default_profile, bindings=tuple(bindings))


__all__ = [
    "Binding",
    "BindingMatch",
    "BindingsConfig",
    "load_bindings",
]
