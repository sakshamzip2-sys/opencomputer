"""WellKnown source — reads a bundled curated manifest of trusted skills.

The manifest is shipped inside the wheel so OC works offline. To update the
catalogue, ship a new release. (A network-fetched manifest can come later
without breaking this offline path.)
"""

from __future__ import annotations

import json
from pathlib import Path

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource


def _bundled_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "well_known_manifest.json"


class WellKnownSource(SkillSource):
    """Reads from a static JSON manifest. Default: bundled with the package."""

    def __init__(self, manifest_path: Path | None = None) -> None:
        self._path = Path(manifest_path) if manifest_path else _bundled_manifest_path()

    @property
    def name(self) -> str:
        return "well-known"

    def _entries(self) -> list[dict]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text())
        return data.get("entries", [])

    def _to_meta(self, entry: dict) -> SkillMeta:
        return SkillMeta(
            identifier=entry["identifier"],
            name=entry["name"],
            description=entry["description"],
            source=self.name,
            version=entry.get("version"),
            author=entry.get("author"),
            tags=tuple(entry.get("tags", [])),
            trust_level=entry.get("trust_level", "community"),
        )

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        q = query.lower()
        out: list[SkillMeta] = []
        for entry in self._entries():
            if q == "" or q in entry["name"].lower() or q in entry["description"].lower():
                out.append(self._to_meta(entry))
            if len(out) >= limit:
                break
        return out

    def inspect(self, identifier: str) -> SkillMeta | None:
        for entry in self._entries():
            if entry["identifier"] == identifier:
                return self._to_meta(entry)
        return None

    def fetch(self, identifier: str) -> SkillBundle | None:
        for entry in self._entries():
            if entry["identifier"] == identifier:
                return SkillBundle(
                    identifier=entry["identifier"],
                    skill_md=entry["skill_md"],
                    files=dict(entry.get("files", {})),
                )
        return None
