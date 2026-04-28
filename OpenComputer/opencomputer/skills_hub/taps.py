"""TapsManager — register/unregister GitHub repos as SkillSources.

A "tap" is a GitHub user/repo whose SKILL.md files are exposed as
discoverable skills in the hub. Stored at
``~/.opencomputer/<profile>/skills/.hub/taps.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO_FORM = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_GITHUB_URL = re.compile(
    r"^https?://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def _normalize(spec: str) -> str:
    spec = spec.strip()
    m = _GITHUB_URL.match(spec)
    if m:
        return m.group(1)
    if _REPO_FORM.match(spec):
        return spec
    raise ValueError(
        f"taps argument {spec!r} is neither user/repo nor a github.com URL"
    )


class TapsManager:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _read(self) -> list[str]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return []
        return list(data.get("taps", []))

    def _write(self, taps: list[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"taps": taps}, indent=2, sort_keys=True))

    def list(self) -> list[str]:
        return self._read()

    def add(self, spec: str) -> None:
        repo = _normalize(spec)
        taps = self._read()
        if repo not in taps:
            taps.append(repo)
            taps.sort()
            self._write(taps)

    def remove(self, spec: str) -> None:
        repo = _normalize(spec)
        taps = [t for t in self._read() if t != repo]
        self._write(taps)
