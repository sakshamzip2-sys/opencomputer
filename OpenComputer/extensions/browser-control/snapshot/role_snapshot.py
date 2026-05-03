"""Role-snapshot Path 2 — Playwright `aria_snapshot()` text → refs.

Input: an ARIA tree text WITHOUT refs:

    - main "Content":
      - button "OK"
      - link "Home"

Output: ``SnapshotResult`` containing the rebuilt snapshot text WITH
``[ref=eN]`` (and ``[nth=N]`` only when role+name collides) plus a
``refs`` map: ``{"e1": RoleRef("main", "Content"), ...}``.

Dedup logic mirrors the worked example in deep-dive §4 — duplicates get
``[nth=0]``, ``[nth=1]``; the visible suffix only appears for ``nth > 0``;
non-duplicate ``nth`` is stripped from the ref map after the walk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from .snapshot_roles import CONTENT_ROLES, INTERACTIVE_ROLES, STRUCTURAL_ROLES

# 2-space indent unit. OpenClaw's ariaSnapshot output is 2-space indented;
# any other emitter (e.g. user-pasted snippet) is normalized via
# math.floor(leading-spaces / 2).
INDENT_UNIT: Final[int] = 2


@dataclass(slots=True)
class RoleRef:
    role: str
    name: str | None = None
    nth: int | None = None


@dataclass(slots=True)
class SnapshotStats:
    lines: int
    chars: int
    refs: int
    interactive: int


@dataclass(slots=True)
class SnapshotResult:
    snapshot: str
    refs: dict[str, RoleRef] = field(default_factory=dict)
    stats: SnapshotStats | None = None
    truncated: bool = False


# ─── parsing ──────────────────────────────────────────────────────────


# Allow letters / digits / hyphens in roleRaw — the wild aria-snapshot
# output sometimes has dashes (`menu-bar`).
_LINE_RE = re.compile(r'^(?P<prefix>\s*-\s*)(?P<role>/?[A-Za-z][A-Za-z0-9_-]*)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')


def _indent_level(line: str) -> int:
    leading = len(line) - len(line.lstrip(" "))
    return leading // INDENT_UNIT


def parse_role_ref(raw: str) -> str | None:
    """Lenient parser. Accepts ``e1``, ``@e1``, ``ref=e1``, ``[ref=e1]``."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Strip surrounding brackets.
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    if s.startswith("ref="):
        s = s[4:]
    if s.startswith("@"):
        s = s[1:]
    if re.fullmatch(r"[A-Za-z0-9_-]+", s):
        return s
    return None


# ─── tracker ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class RoleNameTracker:
    """Counts (role, name) occurrences and tracks which refs share a key.

    Used by ``build_role_snapshot_from_aria_snapshot`` to assign ``nth`` and
    later strip ``nth`` from refs whose key turned out unique.
    """

    counts: dict[str, int] = field(default_factory=dict)
    refs_by_key: dict[str, list[str]] = field(default_factory=dict)

    @staticmethod
    def _key(role: str, name: str | None) -> str:
        return f"{role}:{name or ''}"

    def get_next_index(self, role: str, name: str | None) -> int:
        """Return the next 0-based index for this (role, name) key, then increment."""
        key = self._key(role, name)
        idx = self.counts.get(key, 0)
        self.counts[key] = idx + 1
        return idx

    def track_ref(self, role: str, name: str | None, ref: str) -> None:
        key = self._key(role, name)
        self.refs_by_key.setdefault(key, []).append(ref)

    def is_duplicate(self, role: str, name: str | None) -> bool:
        return self.counts.get(self._key(role, name), 0) >= 2


def _remove_nth_from_non_duplicates(
    refs: dict[str, RoleRef],
    tracker: RoleNameTracker,
) -> None:
    """Strip ``nth`` from refs whose role+name turned out unique post-walk."""
    duplicate_keys = {k for k, c in tracker.counts.items() if c >= 2}
    for ref, role_ref in refs.items():
        key = tracker._key(role_ref.role, role_ref.name)
        if key not in duplicate_keys:
            role_ref.nth = None


# ─── compact-tree pruning ─────────────────────────────────────────────


def _compact_tree(tree: str) -> str:
    """Drop indent blocks that have no ``[ref=...]`` line within them."""
    if not tree:
        return tree
    lines = tree.split("\n")

    # Mark which lines belong to a block that contains at least one ref.
    keep = [False] * len(lines)
    indent_levels = [_indent_level(ln) for ln in lines]
    has_ref = ["[ref=" in ln for ln in lines]
    n = len(lines)
    for i in range(n):
        if has_ref[i]:
            keep[i] = True
            # Mark ancestors (smaller indent) up to root or until we hit
            # another ancestor that's already marked.
            j = i - 1
            cur_lvl = indent_levels[i]
            while j >= 0:
                lvl = indent_levels[j]
                if lvl < cur_lvl:
                    if keep[j]:
                        break
                    keep[j] = True
                    cur_lvl = lvl
                j -= 1
    return "\n".join(line for i, line in enumerate(lines) if keep[i])


# ─── stats ────────────────────────────────────────────────────────────


def _get_stats(snapshot: str, refs: dict[str, RoleRef]) -> SnapshotStats:
    lines = snapshot.count("\n") + 1 if snapshot else 0
    interactive = sum(1 for r in refs.values() if r.role in INTERACTIVE_ROLES)
    return SnapshotStats(lines=lines, chars=len(snapshot), refs=len(refs), interactive=interactive)


# ─── core ─────────────────────────────────────────────────────────────


def build_role_snapshot_from_aria_snapshot(
    aria_snapshot: str,
    *,
    interactive: bool = False,
    max_depth: int | None = None,
    compact: bool = False,
) -> SnapshotResult:
    """Path 2 — Playwright aria text → snapshot text + ref map.

    The classification rules:
      - INTERACTIVE_ROLES always get a ref.
      - CONTENT_ROLES get a ref only when named.
      - STRUCTURAL_ROLES never get a ref; dropped entirely when ``compact`` and unnamed.
      - Anything else is kept verbatim, no ref.

    ``interactive=True`` emits a flat list of just interactive elements.
    """
    refs: dict[str, RoleRef] = {}
    tracker = RoleNameTracker()
    counter = 0

    def _next_ref() -> str:
        nonlocal counter
        counter += 1
        return f"e{counter}"

    if not aria_snapshot:
        return SnapshotResult(snapshot="(empty)", refs={}, stats=_get_stats("", {}))

    out: list[str] = []
    for raw_line in aria_snapshot.split("\n"):
        match = _LINE_RE.match(raw_line)
        if match is None:
            # Pass through any non-aria-list lines (e.g. blank).
            if not interactive:
                out.append(raw_line)
            continue

        role_raw = match.group("role")
        name = match.group("name")  # may be None
        rest = match.group("rest") or ""
        # Closing tag (`/foo`) — keep verbatim, no ref.
        if role_raw.startswith("/"):
            if not interactive:
                out.append(raw_line)
            continue

        depth = _indent_level(raw_line)
        if max_depth is not None and depth > max_depth:
            continue

        role = role_raw.lower()
        is_interactive = role in INTERACTIVE_ROLES
        is_content = role in CONTENT_ROLES
        is_structural = role in STRUCTURAL_ROLES

        if interactive and not is_interactive:
            continue
        if compact and is_structural and not name:
            continue

        should_have_ref = is_interactive or (is_content and bool(name))
        if not should_have_ref:
            if not interactive:
                out.append(raw_line)
            continue

        ref = _next_ref()
        nth = tracker.get_next_index(role, name)
        tracker.track_ref(role, name, ref)
        refs[ref] = RoleRef(role=role, name=name, nth=nth)

        # Build the rendered line. Visible [nth=N] only when N > 0.
        prefix = match.group("prefix")
        name_segment = f' "{name}"' if name is not None else ""
        suffix_parts = [f"[ref={ref}]"]
        if nth > 0:
            suffix_parts.append(f"[nth={nth}]")
        suffix = " ".join(suffix_parts)
        rebuilt = f"{prefix}{role_raw}{name_segment} {suffix}{rest}".rstrip()
        out.append(rebuilt)

    _remove_nth_from_non_duplicates(refs, tracker)

    if interactive:
        snapshot_text = "\n".join(out) if out else "(no interactive elements)"
    else:
        joined = "\n".join(out) if out else "(empty)"
        snapshot_text = _compact_tree(joined) if compact and out else joined

    return SnapshotResult(
        snapshot=snapshot_text,
        refs=refs,
        stats=_get_stats(snapshot_text, refs),
    )
