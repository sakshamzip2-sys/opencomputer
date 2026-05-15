"""MCP tool naming — sanitize, truncate, collision suffix.

mcp-openclaw-port follow-up (Gap D). MCP wire convention caps tool
names at 64 characters; OC's bundle-MCP namespacing produces
``<plugin_id>__<server_name>__<tool_name>`` which can exceed that limit
with realistic IDs. Without truncation the name silently breaks at the
JSON-RPC layer.

This module owns the canonical composition pipeline:

1. **Sanitize** each component — strip / replace any char outside
   ``[A-Za-z0-9_\\-]`` so the name survives a wire round-trip.
2. **Compose** with ``__`` separators. Skip the plugin prefix when
   ``plugin_id`` is empty (user-configured MCPs keep ``<server>__<tool>``
   shape — the M1 plugin-bundle namespacing rule only applies to
   plugin-shipped MCPs).
3. **Truncate** to 64 chars deterministically. Same inputs → same
   output (no hashing — predictable names are easier to debug).
4. **Collision suffix** on conflict. ``-2``, ``-3``, ... up to ``-99``
   (the cap matches OpenClaw's). Beyond that we raise — that many
   collisions on identical truncated names is a structural problem
   (the plugin author should rename).
"""

from __future__ import annotations

import re
from typing import Final

#: Maximum length of an MCP tool name on the wire. 64 is the MCP
#: convention; we cap precisely here so a plugin author shipping
#: long IDs sees a deterministic truncation instead of silent
#: protocol breakage.
MAX_MCP_TOOL_NAME_LEN: Final[int] = 64

#: Regex that every sanitized MCP tool name MUST satisfy. Letters,
#: digits, underscore, hyphen. No dots (could read as ``..``
#: path traversal at a glance), no spaces, no shell metas.
MCP_TOOL_NAME_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-]+$")

#: Cap on collision-suffix escalation. -2 through -99 are tried before
#: raising; we don't extend to -100+ because that many collisions on
#: identical truncated names means the plugin author has a structural
#: problem to fix.
_MAX_COLLISION_SUFFIX: Final[int] = 99


def sanitize_mcp_tool_name(name: str) -> str:
    """Return a wire-safe form of ``name`` (no truncation).

    Each disallowed char becomes ``_``. Empty input → ``_unknown``.
    The result is guaranteed to match :data:`MCP_TOOL_NAME_RE`.
    """
    if not name:
        return "_unknown"
    out_chars: list[str] = []
    for ch in name:
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in "_-":
            out_chars.append(ch)
        else:
            out_chars.append("_")
    sanitized = "".join(out_chars)
    # Defensive post-condition.
    assert MCP_TOOL_NAME_RE.fullmatch(sanitized), (
        f"sanitize regression: {name!r} → {sanitized!r}"
    )
    return sanitized


def truncate_mcp_tool_name(name: str) -> str:
    """Cap ``name`` at :data:`MAX_MCP_TOOL_NAME_LEN` characters.

    Right-truncates (drops trailing characters). Deterministic — same
    input always yields the same output. Idempotent.
    """
    if len(name) <= MAX_MCP_TOOL_NAME_LEN:
        return name
    return name[:MAX_MCP_TOOL_NAME_LEN]


def compose_mcp_tool_name(
    plugin_id: str,
    server_name: str,
    tool_name: str,
    existing: set[str],
) -> str:
    """Build the canonical MCP tool name and reserve it in ``existing``.

    Pipeline:

    1. Sanitize ``plugin_id`` / ``server_name`` / ``tool_name``.
    2. Compose: ``<plugin>__<server>__<tool>`` (skip plugin when empty).
    3. Truncate to :data:`MAX_MCP_TOOL_NAME_LEN`.
    4. On collision in ``existing``, escalate via ``-2``, ``-3``, ...
       up to ``-99``. The collision suffix is added BEFORE the truncate
       so the final name still fits within the cap.
    5. Mutates ``existing`` to add the chosen name so callers can chain
       calls without thinking about state.

    Raises :class:`ValueError` when collision-suffix escalation hits the
    cap (means 99 prior names already mapped to the same truncated base).
    """
    safe_plug = sanitize_mcp_tool_name(plugin_id) if plugin_id else ""
    safe_srv = sanitize_mcp_tool_name(server_name)
    safe_tool = sanitize_mcp_tool_name(tool_name)
    if safe_plug and safe_plug != "_unknown":
        base = f"{safe_plug}__{safe_srv}__{safe_tool}"
    else:
        base = f"{safe_srv}__{safe_tool}"
    base = truncate_mcp_tool_name(base)
    if base not in existing:
        existing.add(base)
        return base
    # Collision — escalate. Suffix shape is ``-<n>``; reserve room for
    # it within the 64-char budget.
    for n in range(2, _MAX_COLLISION_SUFFIX + 1):
        suffix = f"-{n}"
        room = MAX_MCP_TOOL_NAME_LEN - len(suffix)
        candidate = f"{base[:room]}{suffix}"
        if candidate not in existing:
            existing.add(candidate)
            return candidate
    raise ValueError(
        f"MCP tool name collision exceeded {_MAX_COLLISION_SUFFIX} suffixes "
        f"for base {base!r}; rename the plugin / server / tool"
    )


__all__ = [
    "MAX_MCP_TOOL_NAME_LEN",
    "MCP_TOOL_NAME_RE",
    "compose_mcp_tool_name",
    "sanitize_mcp_tool_name",
    "truncate_mcp_tool_name",
]
