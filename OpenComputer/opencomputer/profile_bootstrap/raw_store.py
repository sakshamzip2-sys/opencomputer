"""Content-addressed raw artifact store for Layer 3 deepening.

Each artifact lives at ``<store_root>/<aa>/<bb>/<full-sha256>.json`` where
``<aa><bb>`` is the SHA256 prefix used as a fanout (avoids 100k+ files in
a single dir). The JSON envelope:

    {
      "content_hash": "...",
      "kind": "file" | "email" | "browser_visit" | "git_commit" | ...,
      "source_path": "/Users/.../doc.md",
      "stored_at": <epoch>,
      "content": "<the raw text or summary>"
    }

The store is profile-aware via :func:`opencomputer.agent.config._home`.
Idempotency: identical content produces the same hash, the same path, and
the same envelope (re-storing is a no-op).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from opencomputer.agent.config import _home


@dataclass(frozen=True, slots=True)
class RawStoreEntry:
    """A successfully-stored artifact's envelope + on-disk path."""

    content_hash: str
    kind: str
    source_path: str
    stored_at: float
    path: Path
    content: str = ""


def _default_store_root() -> Path:
    return _home() / "profile_bootstrap" / "raw_store"


def compute_content_hash(content: str | bytes) -> str:
    """SHA256 hex digest of the artifact content. Stable across re-runs."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _path_for_hash(content_hash: str, store_root: Path) -> Path:
    aa, bb = content_hash[:2], content_hash[2:4]
    return store_root / aa / bb / f"{content_hash}.json"


def has_artifact(content_hash: str, *, store_root: Path | None = None) -> bool:
    """Cheap existence probe — no JSON parse."""
    root = store_root if store_root is not None else _default_store_root()
    return _path_for_hash(content_hash, root).exists()


def store_artifact(
    *,
    content: str,
    kind: str,
    source_path: str,
    store_root: Path | None = None,
) -> RawStoreEntry:
    """Write the artifact at its content-addressed path. Idempotent."""
    root = store_root if store_root is not None else _default_store_root()
    h = compute_content_hash(content)
    path = _path_for_hash(h, root)
    if path.exists():
        # Re-read the existing envelope to return the original stored_at.
        return _read_envelope(path) or RawStoreEntry(
            content_hash=h, kind=kind, source_path=source_path,
            stored_at=time.time(), path=path, content=content,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    entry = RawStoreEntry(
        content_hash=h,
        kind=kind,
        source_path=source_path,
        stored_at=time.time(),
        path=path,
        content=content,
    )
    envelope = {
        "content_hash": h,
        "kind": kind,
        "source_path": source_path,
        "stored_at": entry.stored_at,
        "content": content,
    }
    path.write_text(json.dumps(envelope))
    return entry


def read_artifact(
    content_hash: str, *, store_root: Path | None = None,
) -> RawStoreEntry | None:
    """Re-hydrate an envelope by hash. ``None`` if absent."""
    root = store_root if store_root is not None else _default_store_root()
    path = _path_for_hash(content_hash, root)
    if not path.exists():
        return None
    return _read_envelope(path)


def _read_envelope(path: Path) -> RawStoreEntry | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return RawStoreEntry(
        content_hash=str(data.get("content_hash", "")),
        kind=str(data.get("kind", "")),
        source_path=str(data.get("source_path", "")),
        stored_at=float(data.get("stored_at", 0.0)),
        path=path,
        content=str(data.get("content", "")),
    )
