"""Persistent content-hash -> file_id cache for the Anthropic Files API.

Default OFF. Opt-in via:
  - OPENCOMPUTER_ANTHROPIC_FILES_CACHE=1 env var
  - runtime.custom["anthropic_files_cache"] = True

Cache file: <profile_home>/anthropic_files_cache.json
Format: {"<sha256-hex>": {"file_id": "...", "uploaded_at": "<iso>",
                          "filename": "...", "size_bytes": N}}

Content-addressed: same bytes always hash to the same file_id, so cache
is safe across processes (last writer wins on race; both writers had
the same data).

Failure-open: any cache I/O error is logged + treated as a miss.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

CACHE_FILENAME = "anthropic_files_cache.json"


@dataclass
class CacheEntry:
    """One cache entry — what we know about a previously-uploaded file."""

    file_id: str
    uploaded_at: str            # ISO8601
    filename: str
    size_bytes: int


def hash_file_bytes(data: bytes) -> str:
    """SHA-256 hex digest of file bytes — used as cache key."""
    return hashlib.sha256(data).hexdigest()


class FilesCache:
    """JSON-backed content-hash -> file_id cache. Failure-open."""

    def __init__(self, cache_path: Path) -> None:
        self.path = cache_path

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("FilesCache read failed (%s); treating as empty", exc)
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except OSError as exc:
            _log.warning("FilesCache write failed (%s); cache miss next time", exc)

    def get(self, content_hash: str) -> CacheEntry | None:
        """Return the cached entry, or None if missing or malformed."""
        data = self._load()
        entry = data.get(content_hash)
        if entry is None:
            return None
        try:
            return CacheEntry(**entry)
        except TypeError as exc:
            _log.warning(
                "FilesCache entry malformed for %s (%s); ignoring",
                content_hash[:8], exc,
            )
            return None

    def put(
        self,
        content_hash: str,
        *,
        file_id: str,
        filename: str,
        size_bytes: int,
    ) -> None:
        """Store an entry; overwrites any existing entry for this hash."""
        data = self._load()
        data[content_hash] = {
            "file_id": file_id,
            "uploaded_at": datetime.now(UTC).isoformat(),
            "filename": filename,
            "size_bytes": size_bytes,
        }
        self._save(data)

    def invalidate(self, content_hash: str) -> None:
        """Drop an entry — used when server returns 404 for a cached file_id."""
        data = self._load()
        if content_hash in data:
            del data[content_hash]
            self._save(data)


__all__ = [
    "CACHE_FILENAME",
    "CacheEntry",
    "FilesCache",
    "hash_file_bytes",
]
