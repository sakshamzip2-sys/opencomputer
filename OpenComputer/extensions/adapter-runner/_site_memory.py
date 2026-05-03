"""Per-site, per-profile knowledge base for adapter authoring + runs.

Mirrors OpenCLI's directory layout verbatim::

    ~/.opencomputer/<profile>/sites/<site>/
    ├── endpoints.json     # discovered API endpoints, shape, params
    ├── field-map.json     # opaque-field → human-meaning translations
    ├── notes.md           # free-form running notes
    ├── verify/<name>.json # verification fixture per adapter command
    └── fixtures/<name>-<ts>.json  # captured sample responses

The two JSON files are read/write; ``notes.md`` is append-only from the
agent's perspective (humans edit freely). Concurrency is handled via
the ``plugin_sdk.file_lock.exclusive_lock`` sidecar pattern — multiple
adapter instances racing on the same profile won't shred each other's
writes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugin_sdk.file_lock import exclusive_lock


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning ``{}`` for missing/empty/corrupt."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Atomic write with the sidecar-lock trick from ``file_lock``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with exclusive_lock(path):
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)


@dataclass(slots=True)
class SiteMemory:
    """Read/write API for a single ``<profile>/sites/<site>/`` directory.

    Construct via ``SiteMemory.for_site(profile_home, site)``; the
    instance caches the resolved root and lazily ensures it exists on
    first write.
    """

    root: Path

    @classmethod
    def for_site(cls, profile_home: Path, site: str) -> SiteMemory:
        return cls(root=Path(profile_home) / "sites" / site)

    # ─── endpoints.json ─────────────────────────────────────────

    @property
    def endpoints_path(self) -> Path:
        return self.root / "endpoints.json"

    def read_endpoints(self) -> dict[str, Any]:
        return _load_json(self.endpoints_path)

    def write_endpoint(self, key: str, entry: dict[str, Any]) -> None:
        """Upsert a single endpoint entry. Stamps ``verified_at`` if absent."""
        data = self.read_endpoints()
        if "verified_at" not in entry:
            entry = dict(entry)
            entry["verified_at"] = time.strftime("%Y-%m-%d")
        data[key] = entry
        _save_json_atomic(self.endpoints_path, data)

    def get_endpoint(self, key: str) -> dict[str, Any] | None:
        return self.read_endpoints().get(key)

    # ─── field-map.json ─────────────────────────────────────────

    @property
    def field_map_path(self) -> Path:
        return self.root / "field-map.json"

    def read_field_map(self) -> dict[str, Any]:
        return _load_json(self.field_map_path)

    def write_field(self, name: str, meaning: dict[str, Any]) -> None:
        data = self.read_field_map()
        if "verified_at" not in meaning:
            meaning = dict(meaning)
            meaning["verified_at"] = time.strftime("%Y-%m-%d")
        data[name] = meaning
        _save_json_atomic(self.field_map_path, data)

    # ─── notes.md ──────────────────────────────────────────────

    @property
    def notes_path(self) -> Path:
        return self.root / "notes.md"

    def read_notes(self) -> str:
        if not self.notes_path.exists():
            return ""
        try:
            return self.notes_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def append_note(self, line: str) -> None:
        """Append a single note line, prefixed with today's ISO date."""
        self.notes_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d")
        text = f"- {stamp}: {line.rstrip()}\n"
        with exclusive_lock(self.notes_path):
            with self.notes_path.open("a", encoding="utf-8") as fh:
                fh.write(text)

    # ─── verify/<name>.json ────────────────────────────────────

    def verify_path(self, name: str) -> Path:
        return self.root / "verify" / f"{name}.json"

    def read_verify(self, name: str) -> dict[str, Any] | None:
        path = self.verify_path(name)
        if not path.exists():
            return None
        return _load_json(path) or None

    def write_verify(self, name: str, fixture: dict[str, Any]) -> None:
        path = self.verify_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_json_atomic(path, fixture)

    # ─── fixtures/<name>-<ts>.json ─────────────────────────────

    def fixture_path(self, name: str, timestamp: str | None = None) -> Path:
        ts = timestamp or time.strftime("%Y%m%d-%H%M%S")
        return self.root / "fixtures" / f"{name}-{ts}.json"

    def write_fixture(
        self, name: str, payload: Any, *, timestamp: str | None = None
    ) -> Path:
        """Persist a sample API response. Returns the written path."""
        path = self.fixture_path(name, timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Fixtures aren't dicts — use a simpler write path.
        with exclusive_lock(path):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            tmp.replace(path)
        return path

    # ─── generic key/value ─────────────────────────────────────

    def read(self, key: str) -> Any:
        """Convenience: read a single endpoint or field value by key.

        Used by ``AdapterContext.site_memory.read("token")`` etc.
        """
        endpoints = self.read_endpoints()
        if key in endpoints:
            return endpoints[key]
        field_map = self.read_field_map()
        if key in field_map:
            return field_map[key]
        return None

    def write(self, key: str, value: Any) -> None:
        """Convenience: write to endpoints.json (the most common case)."""
        if not isinstance(value, dict):
            value = {"value": value}
        self.write_endpoint(key, value)


__all__ = ["SiteMemory"]
