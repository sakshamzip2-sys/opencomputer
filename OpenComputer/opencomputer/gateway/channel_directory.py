"""
Channel directory — persistent {(platform, chat_id) → display_name} cache.

Task II.3 — mirrors Hermes's ``sources/hermes-agent/gateway/channel_directory.py``
at a lighter weight. Every inbound ``MessageEvent`` has its sender's display
name (when the adapter provides one) cached here so future send-message tools
and listings can resolve friendly names instead of raw numeric chat ids.

Storage shape on disk — ``~/.opencomputer/channel_directory.json``:

    {
        "telegram:12345": {
            "platform": "telegram",
            "chat_id": "12345",
            "display_name": "Saksham",
            "last_seen": 1737676800.0
        },
        ...
    }

The JSON layout is a flat map keyed by ``"{platform}:{chat_id}"``. Writes are
atomic: the new content lands in ``<path>.tmp`` first, then ``os.replace``s
over the primary. A crash between those two steps leaves the primary untouched
(or absent on first run) — never half-written.

Malformed JSON on load is tolerated: we emit a WARNING and treat the directory
as empty, so a corrupted file can't take down the gateway startup path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from opencomputer.agent.config import _home

logger = logging.getLogger("opencomputer.gateway.channel_directory")


@dataclass(frozen=True, slots=True)
class ChannelEntry:
    """One remembered channel — the unit persisted per ``(platform, chat_id)``."""

    platform: str  # "telegram", "discord", "slack", ...
    chat_id: str  # string form so numeric + alphanumeric ids round-trip
    display_name: str | None
    last_seen: float  # unix timestamp, updated on every ``record`` call


def _composite_key(platform: str, chat_id: str) -> str:
    """Stable key for the on-disk dict. Colon-delimited, matches Hermes target strings."""
    return f"{platform}:{chat_id}"


class ChannelDirectory:
    """Mutable, file-backed cache of channel metadata.

    Thread safety: not protected. Gateway callers run in a single asyncio
    loop and the ``record`` path is best-effort; concurrent mutation is not
    part of this module's contract. See Hermes's implementation, which has
    the same shape.
    """

    def __init__(self, path: Path | None = None) -> None:
        # Default: sit next to config.yaml under ~/.opencomputer (or
        # OPENCOMPUTER_HOME when the env var is set — ``_home()`` handles
        # both).
        self.path: Path = path if path is not None else _home() / "channel_directory.json"
        self._entries: dict[str, ChannelEntry] = self._load()

    # ── mutation ───────────────────────────────────────────────────────

    def record(
        self,
        platform: str,
        chat_id: str,
        display_name: str | None = None,
    ) -> None:
        """Record or refresh an entry.

        First-time ids get the provided ``display_name`` (or ``None`` if the
        adapter didn't supply one). Subsequent records for the same
        ``(platform, chat_id)`` always bump ``last_seen`` and overwrite
        ``display_name`` only when a new non-None value is provided —
        protecting names already stored from later events that omit them.
        """
        key = _composite_key(platform, chat_id)
        existing = self._entries.get(key)
        effective_name = display_name if display_name is not None else (
            existing.display_name if existing is not None else None
        )
        entry = ChannelEntry(
            platform=platform,
            chat_id=chat_id,
            display_name=effective_name,
            last_seen=time.time(),
        )
        self._entries[key] = entry
        self._save()

    # ── read ───────────────────────────────────────────────────────────

    def get(self, platform: str, chat_id: str) -> ChannelEntry | None:
        """Fetch one entry, or ``None`` when ``(platform, chat_id)`` is unknown."""
        return self._entries.get(_composite_key(platform, chat_id))

    def list_all(self) -> list[ChannelEntry]:
        """Return all entries sorted by most-recent ``last_seen`` first."""
        return sorted(
            self._entries.values(), key=lambda e: e.last_seen, reverse=True
        )

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> dict[str, ChannelEntry]:
        """Read the JSON file into memory. Missing or corrupt → empty dict.

        Corruption is logged at WARNING so operators see it; we don't raise
        because a garbled directory file must not block the gateway.
        """
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "channel_directory: malformed or unreadable file at %s (%s); treating as empty",
                self.path,
                e,
            )
            return {}
        if not isinstance(raw, dict):
            logger.warning(
                "channel_directory: unexpected top-level shape at %s (%r); treating as empty",
                self.path,
                type(raw).__name__,
            )
            return {}

        entries: dict[str, ChannelEntry] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                entries[key] = ChannelEntry(
                    platform=str(value["platform"]),
                    chat_id=str(value["chat_id"]),
                    display_name=(
                        str(value["display_name"])
                        if value.get("display_name") is not None
                        else None
                    ),
                    last_seen=float(value.get("last_seen", 0.0)),
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(
                    "channel_directory: skipping malformed entry %r: %s", key, e
                )
        return entries

    def _save(self) -> None:
        """Persist entries atomically — tmp + os.replace.

        ``os.replace`` is atomic on POSIX and same-volume on Windows (always
        our case — sibling of ``self.path``). A crash between the tmp write
        and the replace leaves the primary file untouched.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {key: asdict(entry) for key, entry in self._entries.items()}
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


__all__ = ["ChannelDirectory", "ChannelEntry"]
