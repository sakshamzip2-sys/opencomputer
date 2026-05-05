"""Markdown-files-on-disk wiki memory (Phase 12d.4 / C.2 MVP, 2026-05-05).

Storage layout::

    <profile-home>/wiki/
        slug-1.md       ← YAML frontmatter + body
        slug-2.md
        .backlinks.json ← computed cache: target-slug → [referrer-slug, ...]

Each ``slug.md`` looks like::

    ---
    title: My note
    created_at: 1714867200
    updated_at: 1714867200
    tags: [a, b]
    ---
    body markdown with [[other-slug]] links auto-extracted

MVP scope: add/read/search/delete/backlinks. Conflict resolution across
simultaneous edits + cross-profile sharing explicitly out of scope.

Search uses ``rg`` when available (stdlib ``shutil.which``); falls back
to in-memory regex grep otherwise.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9_-]{0,127})\]\]", re.IGNORECASE)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Note:
    """One wiki note."""

    slug: str
    title: str
    body: str
    tags: tuple[str, ...]
    created_at: int
    updated_at: int


# ─── Slug helpers ─────────────────────────────────────────────────────


def slugify(title: str) -> str:
    """Best-effort slugify: lowercase + dashes only. No collision check here."""
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if not s:
        s = "untitled"
    return s[:128]


def validate_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug))


# ─── Frontmatter (YAML-flavoured but parsed simply to avoid yaml dep) ──


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text, body = m.group(1), m.group(2)
    out: dict = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            out[key] = [
                item.strip().strip('"').strip("'")
                for item in inner.split(",")
                if item.strip()
            ]
        else:
            out[key] = val.strip().strip('"').strip("'")
    return out, body


def _format_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k in ("title", "created_at", "updated_at", "tags"):
        if k not in meta:
            continue
        v = meta[k]
        if isinstance(v, list):
            inner = ", ".join(json.dumps(x) for x in v)
            lines.append(f"{k}: [{inner}]")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {json.dumps(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ─── Wikilinks ────────────────────────────────────────────────────────


def extract_wikilinks(body: str) -> list[str]:
    """Return target slugs referenced by ``[[slug]]`` patterns. Deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(body):
        slug = m.group(1).lower()
        if slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


# ─── Backend ──────────────────────────────────────────────────────────


class WikiMemoryBackend:
    """Markdown-on-disk wiki backend."""

    def __init__(self, *, root: Path) -> None:
        self.root = root
        self._backlinks_path = root / ".backlinks.json"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # ─── Path helpers ───────────────────────────────────────────────

    def _path_for(self, slug: str) -> Path:
        return self.root / f"{slug}.md"

    def _resolve_unique_slug(self, base: str) -> str:
        """If ``base`` exists, append ``-2``, ``-3``, … until free."""
        if not self._path_for(base).exists():
            return base
        i = 2
        while self._path_for(f"{base}-{i}").exists():
            i += 1
        return f"{base}-{i}"

    # ─── Public API ─────────────────────────────────────────────────

    def add(
        self,
        *,
        title: str,
        body: str,
        tags: tuple[str, ...] = (),
        slug: str | None = None,
    ) -> str:
        """Write a new note. Returns the assigned slug."""
        self._ensure_root()
        chosen_slug = slug or slugify(title)
        if not validate_slug(chosen_slug):
            raise ValueError(f"invalid slug: {chosen_slug!r}")
        chosen_slug = self._resolve_unique_slug(chosen_slug)

        now = int(time.time())
        meta = {
            "title": title,
            "created_at": now,
            "updated_at": now,
            "tags": list(tags),
        }
        text = _format_frontmatter(meta) + body
        self._path_for(chosen_slug).write_text(text, encoding="utf-8")
        self._update_backlinks_for(chosen_slug, body)
        return chosen_slug

    def read(self, slug: str) -> Note | None:
        """Read a note by slug. Returns None if missing."""
        path = self._path_for(slug)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        return Note(
            slug=slug,
            title=str(meta.get("title", slug)),
            body=body,
            tags=tuple(meta.get("tags", []) or []),
            created_at=int(meta.get("created_at", 0) or 0),
            updated_at=int(meta.get("updated_at", 0) or 0),
        )

    def delete(self, slug: str) -> bool:
        """Delete a note + clean its outgoing backlink entries."""
        path = self._path_for(slug)
        if not path.exists():
            return False
        # Strip from the backlinks index.
        backlinks = self._read_backlinks_index()
        for target in list(backlinks.keys()):
            referrers = [r for r in backlinks[target] if r != slug]
            if referrers:
                backlinks[target] = referrers
            else:
                del backlinks[target]
        path.unlink()
        self._write_backlinks_index(backlinks)
        return True

    def search(self, query: str) -> list[str]:
        """Return slugs whose body OR title contains the query string.

        Prefers ``rg`` (ripgrep) when available; falls back to in-memory
        regex search.
        """
        self._ensure_root()
        if shutil.which("rg") is not None:
            return self._search_via_rg(query)
        return self._search_in_memory(query)

    def backlinks(self, slug: str) -> list[str]:
        """Return slugs that reference ``slug`` via ``[[slug]]`` syntax."""
        return list(self._read_backlinks_index().get(slug, []))

    def list_slugs(self) -> list[str]:
        """List every note slug in the wiki (sorted)."""
        if not self.root.exists():
            return []
        out = sorted(p.stem for p in self.root.glob("*.md"))
        return out

    # ─── Internal: search backends ──────────────────────────────────

    def _search_via_rg(self, query: str) -> list[str]:
        try:
            proc = subprocess.run(
                [
                    "rg", "-l", "--type", "md", "--ignore-case",
                    "--", query, str(self.root),
                ],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return self._search_in_memory(query)
        slugs: list[str] = []
        for line in (proc.stdout or "").splitlines():
            p = Path(line.strip())
            if p.suffix == ".md":
                slugs.append(p.stem)
        return sorted(set(slugs))

    def _search_in_memory(self, query: str) -> list[str]:
        q = query.lower()
        out: list[str] = []
        for path in self.root.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8").lower()
            except OSError:
                continue
            if q in text:
                out.append(path.stem)
        return sorted(out)

    # ─── Internal: backlinks index ──────────────────────────────────

    def _read_backlinks_index(self) -> dict[str, list[str]]:
        if not self._backlinks_path.exists():
            return {}
        try:
            raw = json.loads(self._backlinks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return {
            str(k): [str(x) for x in v if isinstance(x, str)]
            for k, v in (raw or {}).items()
            if isinstance(v, list)
        }

    def _write_backlinks_index(self, index: dict[str, list[str]]) -> None:
        self._ensure_root()
        # Stable sort within each list for diff-friendly storage.
        sorted_index = {k: sorted(set(v)) for k, v in index.items()}
        self._backlinks_path.write_text(
            json.dumps(sorted_index, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _update_backlinks_for(self, referrer_slug: str, body: str) -> None:
        targets = extract_wikilinks(body)
        index = self._read_backlinks_index()
        for target in targets:
            referrers = index.setdefault(target, [])
            if referrer_slug not in referrers:
                referrers.append(referrer_slug)
        self._write_backlinks_index(index)


__all__ = [
    "Note",
    "WikiMemoryBackend",
    "extract_wikilinks",
    "slugify",
    "validate_slug",
]
