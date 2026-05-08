"""Checkpoint — a content-hashed snapshot of some files at a point in time."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class Checkpoint:
    """Immutable snapshot. ``id`` is a SHA-256 digest of sorted ``(path, bytes)`` pairs.

    ``excluded_files`` records paths that were skipped at snapshot time
    (e.g. exceeded a per-file size cap). Skipped files do NOT contribute
    to the hash. Restoring a Checkpoint with non-empty
    ``excluded_files`` leaves those paths on disk untouched — the user
    can decide whether to back them up separately.
    """

    id: str
    files: Mapping[str, bytes]
    label: str
    created_at: str  # ISO 8601 UTC
    excluded_files: tuple[str, ...] = field(default_factory=tuple)

    @staticmethod
    def from_files(
        files: Mapping[str, bytes],
        *,
        label: str,
        max_file_size_bytes: int | None = None,
    ) -> Checkpoint:
        """Build a :class:`Checkpoint`, optionally excluding files above a size cap.

        Args:
            files: ``path → bytes`` map.
            label: human-readable label (e.g. ``"before Edit"``).
            max_file_size_bytes: when set, files exceeding this size are
                EXCLUDED from ``files`` and recorded in
                ``excluded_files``. The hash is computed only over
                included files.
        """
        included: dict[str, bytes] = {}
        excluded: list[str] = []
        for path, data in files.items():
            if max_file_size_bytes is not None and len(data) > max_file_size_bytes:
                excluded.append(path)
                continue
            included[path] = data

        h = hashlib.sha256()
        for path in sorted(included):
            h.update(path.encode("utf-8"))
            h.update(b"\x00")
            h.update(included[path])
            h.update(b"\x00")

        return Checkpoint(
            id=h.hexdigest()[:16],
            files=included,
            label=label,
            created_at=datetime.now(UTC).isoformat(),
            excluded_files=tuple(excluded),
        )


__all__ = ["Checkpoint"]
