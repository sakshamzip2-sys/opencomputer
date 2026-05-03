"""Chrome MCP tree → unified ``SnapshotResult`` (Path 3).

Input: a ``ChromeMcpSnapshotNode`` JSON tree returned by
``take_snapshot``. Each node carries an opaque ``id`` (the *uid*) which
becomes the snapshot ``ref`` directly — no allocation table needed since
``click``/``fill``/etc. accept the same uid.

Output shape matches Path 2 (``SnapshotResult``) so the caller can treat
all snapshot pipelines uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .role_snapshot import (
    RoleRef,
    SnapshotResult,
    _get_stats,
)
from .snapshot_roles import CONTENT_ROLES, INTERACTIVE_ROLES, STRUCTURAL_ROLES


@dataclass(slots=True)
class ChromeMcpSnapshotNode:
    id: str | None = None
    role: str | None = None
    name: str | None = None
    value: str | None = None
    description: str | None = None
    children: list[ChromeMcpSnapshotNode] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> ChromeMcpSnapshotNode:
        if not isinstance(data, dict):
            return cls()
        children_raw = data.get("children")
        children: list[ChromeMcpSnapshotNode] = []
        if isinstance(children_raw, list):
            for c in children_raw:
                children.append(cls.from_dict(c))

        def _str_or_none(v: Any) -> str | None:
            return v if isinstance(v, str) and v else None

        return cls(
            id=_str_or_none(data.get("id")),
            role=_str_or_none(data.get("role")),
            name=_str_or_none(data.get("name")),
            value=_str_or_none(data.get("value")),
            description=_str_or_none(data.get("description")),
            children=children,
        )


def _normalize_role(node: ChromeMcpSnapshotNode) -> str:
    if not node.role:
        return "generic"
    return node.role.lower().strip() or "generic"


def _escape_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _should_include_node(
    role: str,
    name: str | None,
    *,
    interactive: bool,
    compact: bool,
) -> bool:
    is_interactive = role in INTERACTIVE_ROLES
    is_structural = role in STRUCTURAL_ROLES
    if interactive and not is_interactive:
        return False
    return not (compact and is_structural and not name)


def _should_create_ref(role: str, name: str | None) -> bool:
    return role in INTERACTIVE_ROLES or (role in CONTENT_ROLES and bool(name))


@dataclass(slots=True)
class _DuplicateTracker:
    counts: dict[str, int] = field(default_factory=dict)
    keys_by_ref: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _key(role: str, name: str | None) -> str:
        return f"{role}:{name or ''}"

    def register(self, ref: str, role: str, name: str | None) -> int | None:
        """Return ``None`` on first occurrence, else the (1-based) index of this dup."""
        key = self._key(role, name)
        n = self.counts.get(key, 0)
        self.counts[key] = n + 1
        self.keys_by_ref[ref] = key
        return None if n == 0 else n


def flatten_chrome_mcp_snapshot(
    root: ChromeMcpSnapshotNode | dict[str, Any],
    *,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """DFS-flatten a Chrome MCP tree to ``[{depth, role, name, ref?, ...}, ...]``."""
    boundedLimit = max(1, min(2000, int(limit)))  # noqa: N806 — match deep-dive name
    if isinstance(root, dict):
        root = ChromeMcpSnapshotNode.from_dict(root)
    out: list[dict[str, Any]] = []
    stack: list[tuple[ChromeMcpSnapshotNode, int]] = [(root, 0)]
    while stack and len(out) < boundedLimit:
        node, depth = stack.pop()
        out.append(
            {
                "depth": depth,
                "id": node.id,
                "role": _normalize_role(node),
                "name": node.name,
                "value": node.value,
                "description": node.description,
            }
        )
        for child in reversed(node.children):
            stack.append((child, depth + 1))
    return out


def build_ai_snapshot_from_chrome_mcp_snapshot(
    root: ChromeMcpSnapshotNode | dict[str, Any],
    *,
    interactive: bool = False,
    compact: bool = False,
    max_depth: int | None = None,
    max_chars: int | None = None,
) -> SnapshotResult:
    """Path 3 — Chrome MCP tree → ``SnapshotResult``."""
    if isinstance(root, dict):
        root = ChromeMcpSnapshotNode.from_dict(root)

    refs: dict[str, RoleRef] = {}
    tracker = _DuplicateTracker()
    lines: list[str] = []

    def visit(node: ChromeMcpSnapshotNode, depth: int) -> None:
        if max_depth is not None and depth > max_depth:
            return
        role = _normalize_role(node)
        name = node.name
        value = node.value
        description = node.description

        if _should_include_node(role, name, interactive=interactive, compact=compact):
            indent = "  " * depth
            line = f"{indent}- {role}"
            if name:
                line += f' "{_escape_quoted(name)}"'
            ref = node.id
            if ref and _should_create_ref(role, name):
                nth = tracker.register(ref, role, name)
                if nth is None:
                    refs[ref] = RoleRef(role=role, name=name, nth=None)
                else:
                    refs[ref] = RoleRef(role=role, name=name, nth=nth)
                line += f" [ref={ref}]"
            if value:
                line += f' value="{_escape_quoted(value)}"'
            if description:
                line += f' description="{_escape_quoted(description)}"'
            lines.append(line)

        for child in node.children:
            visit(child, depth + 1)

    visit(root, 0)

    # Strip nth from refs whose role+name turned out unique.
    duplicate_keys = {k for k, c in tracker.counts.items() if c >= 2}
    for ref, role_ref in refs.items():
        key = tracker.keys_by_ref.get(ref)
        if key is None or key not in duplicate_keys:
            role_ref.nth = None

    snapshot = "\n".join(lines)
    truncated = False
    if max_chars is not None and len(snapshot) > max_chars:
        snapshot = snapshot[:max_chars] + "\n\n[...TRUNCATED - page too large]"
        truncated = True

    stats = _get_stats(snapshot, refs)
    return SnapshotResult(snapshot=snapshot, refs=refs, stats=stats, truncated=truncated)
