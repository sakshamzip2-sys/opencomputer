"""Broadcast Groups — fan-out one inbound message to many agents.

Port of OpenClaw's ``broadcastGroups`` config (see
``docs/OC-FROM-OPENCLAW.md`` item 10). A single inbound message
matching a configured chat id triggers all listed agent profiles in
parallel; each agent runs in its own session with isolated history
and workspace.

Config shape (``~/.opencomputer/<root>/broadcast.yaml`` or the
``broadcast_groups:`` key in ``config.yaml``)::

    broadcast_groups:
      "telegram://-1001234567":
        - work-profile
        - research-profile
        - monitor-profile

The map key is a ``<channel>://<chat-id>`` URI so groups can scope per
channel (Telegram chat id ``-100…`` is different from a Discord
guild). The value is an ordered list of profile / agent ids to fan
out to.

Key design choice — **the gateway** is the dispatch site, not the
agent loop. The fan-out happens at the inbound-event boundary so each
agent sees a normal single-message turn from its own perspective. The
agent runs are otherwise independent: no shared session, no shared
context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)

#: Canonical config field name (matches OpenClaw spelling).
BROADCAST_GROUPS_FIELD: str = "broadcast_groups"
broadcastGroups: str = BROADCAST_GROUPS_FIELD  # noqa: N816 — camelCase alias mirrors OpenClaw spec spelling


@dataclass(frozen=True, slots=True)
class BroadcastGroup:
    """One group: a routing key (channel://chat-id) → ordered agent ids."""

    key: str
    agent_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError(
                f"broadcast group key must be a non-empty string, got {self.key!r}"
            )
        if not self.agent_ids:
            raise ValueError(
                f"broadcast group {self.key!r}: agent_ids must be non-empty"
            )
        seen: set[str] = set()
        for aid in self.agent_ids:
            if not aid or not isinstance(aid, str):
                raise ValueError(
                    f"broadcast group {self.key!r}: agent id must be a "
                    f"non-empty string, got {aid!r}"
                )
            if aid in seen:
                raise ValueError(
                    f"broadcast group {self.key!r}: duplicate agent id {aid!r}"
                )
            seen.add(aid)


@dataclass(frozen=True, slots=True)
class BroadcastConfig:
    """All configured broadcast groups."""

    groups: dict[str, BroadcastGroup] = field(default_factory=dict)

    def lookup(self, key: str) -> BroadcastGroup | None:
        """Return the group for ``key`` or ``None`` when not configured."""
        return self.groups.get(key)

    def lookup_for(self, channel: str, chat_id: str) -> BroadcastGroup | None:
        """Convenience: build ``<channel>://<chat-id>`` and look it up.

        Both args must be non-empty strings; otherwise ``None``.
        """
        if not (channel and isinstance(channel, str)):
            return None
        if not (chat_id and isinstance(chat_id, str)):
            return None
        return self.groups.get(f"{channel}://{chat_id}")

    def keys(self) -> list[str]:
        """Sorted list of configured group keys — stable for display."""
        return sorted(self.groups)


def parse_broadcast_config(raw: Any) -> BroadcastConfig:
    """Parse a free-form mapping into a typed :class:`BroadcastConfig`.

    Malformed entries are logged + skipped — never raise. ``None`` /
    missing → empty config.
    """
    if not isinstance(raw, dict):
        return BroadcastConfig()
    out: dict[str, BroadcastGroup] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not key.strip():
            _log.warning("broadcast_groups: skipping non-string key %r", key)
            continue
        if not isinstance(val, list):
            _log.warning(
                "broadcast_groups: skipping %r — value must be a list, got %s",
                key,
                type(val).__name__,
            )
            continue
        cleaned: list[str] = []
        for entry in val:
            if isinstance(entry, str) and entry.strip():
                if entry not in cleaned:
                    cleaned.append(entry)
        if not cleaned:
            _log.warning("broadcast_groups: skipping %r — no valid agent ids", key)
            continue
        try:
            out[key] = BroadcastGroup(key=key, agent_ids=tuple(cleaned))
        except ValueError as exc:
            _log.warning("broadcast_groups: skipping %r: %s", key, exc)
    return BroadcastConfig(groups=out)


def load_broadcast_config(path: Path | None) -> BroadcastConfig:
    """Load + parse ``broadcast.yaml`` from disk. Missing file → empty.

    Errors are logged + treated as empty — never raise. The gateway
    must boot even with a malformed broadcast config.
    """
    if path is None or not path.exists():
        return BroadcastConfig()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("broadcast_groups: cannot read %s: %s", path, exc)
        return BroadcastConfig()
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        _log.warning("broadcast_groups: %s: invalid YAML: %s", path, exc)
        return BroadcastConfig()
    if not isinstance(raw, dict):
        return BroadcastConfig()
    # Top-level shape: either { "broadcast_groups": { ... } } or
    # raw map of group keys directly. Both supported.
    body = raw.get(BROADCAST_GROUPS_FIELD, raw)
    return parse_broadcast_config(body)


__all__ = [
    "BROADCAST_GROUPS_FIELD",
    "broadcastGroups",
    "BroadcastConfig",
    "BroadcastGroup",
    "load_broadcast_config",
    "parse_broadcast_config",
]
