"""Persistent file_unique_id -> vision-description LRU cache.

OrderedDict with move_to_end on get for recency. Atomic JSON write.
Single-writer assumption (one bot per profile per process).

Hermes channel-port PR 3a.5 / audit C6: lives in plugin_sdk so other
adapters that grow sticker / inline-emoji handling can reuse the same
shape without hopping the opencomputer boundary.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path


class StickerCache:
    """Bounded persistent map: file_unique_id -> human description.

    Telegram returns the same ``file_unique_id`` for a sticker across
    sends regardless of the per-message ``file_id``, so caching by
    unique id lets us short-circuit the vision-describe step on
    recurring stickers (one of the most-repeated payloads on Telegram).
    """

    def __init__(self, profile_home: Path, max_entries: int = 5000) -> None:
        self._path = Path(profile_home) / "sticker_descriptions.json"
        self._max = max_entries
        self._data: OrderedDict[str, str] = self._load()

    def _load(self) -> OrderedDict[str, str]:
        try:
            raw = json.loads(self._path.read_text())
            if isinstance(raw, list):
                # Stored as [[k, v], ...] — preserves insertion order.
                return OrderedDict(
                    (str(k), str(v)) for k, v in raw[-self._max:]
                )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return OrderedDict()

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(list(self._data.items())))
            tmp.replace(self._path)
        except OSError:
            pass

    def get(self, file_unique_id: str) -> str | None:
        if file_unique_id in self._data:
            self._data.move_to_end(file_unique_id)
            return self._data[file_unique_id]
        return None

    def put(self, file_unique_id: str, description: str) -> None:
        self._data[file_unique_id] = description
        self._data.move_to_end(file_unique_id)
        while len(self._data) > self._max:
            self._data.popitem(last=False)
        self._save()


__all__ = ["StickerCache"]
