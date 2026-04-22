"""Checkpoint — a content-hashed snapshot of some files at a point in time."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Checkpoint:
    """Immutable snapshot. `id` is a SHA-256 digest of sorted (path, bytes) pairs."""

    id: str
    files: Mapping[str, bytes]
    label: str
    created_at: str  # ISO 8601 UTC

    @staticmethod
    def from_files(files: Mapping[str, bytes], *, label: str) -> Checkpoint:
        h = hashlib.sha256()
        for path in sorted(files):
            h.update(path.encode("utf-8"))
            h.update(b"\x00")
            h.update(files[path])
            h.update(b"\x00")
        return Checkpoint(
            id=h.hexdigest()[:16],
            files=dict(files),
            label=label,
            created_at=datetime.now(UTC).isoformat(),
        )


__all__ = ["Checkpoint"]
