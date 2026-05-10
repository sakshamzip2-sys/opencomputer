"""GET /api/v1/memory/status — REST mirror of the wire ``memory.status`` RPC.

Tier-C+ of 2026-05-10 memory-observability follow-through. The wire RPC at
``opencomputer.gateway.wire_server`` lets a TUI client seed its memory panel
on connect; this REST endpoint gives the dashboard SPA the same affordance
without forcing it to also speak the WS wire.

Failure isolation mirrors the wire helper:

* Config load fails or active profile has no MemoryConfig → 200 with empty
  ``entries`` list. Empty is a valid state, not an error — clients render
  a hidden panel.
* One file unreadable (permissions etc.) → that entry is omitted, the other
  is reported, request still returns 200. Per-file failure is logged at WARN.
* Both files unreadable → 200 with empty entries. Logged at WARN per file.

Response shape matches :class:`opencomputer.gateway.protocol_v2.MemoryStatusResult`
exactly so downstream consumers can use the same TypeScript / Python types
across SSE, WS, and REST surfaces.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from opencomputer.agent.memory_cap import cap_status

router = APIRouter(prefix="/api/v1", tags=["memory"])
logger = logging.getLogger("opencomputer.dashboard.routes.memory")


def _load_memory_config() -> Any | None:
    """Return the active profile's MemoryConfig, or None if unavailable.

    Wrapped so test code can monkeypatch a stub config without dragging in
    the entire profile resolver. Returns ``None`` rather than raising so
    the route handler can produce a clean empty response.
    """
    try:
        from opencomputer.agent.config_store import load_config
    except Exception:
        logger.warning(
            "memory.status: load_config import failed — returning empty entries",
            exc_info=True,
        )
        return None
    try:
        cfg = load_config()
    except Exception:
        logger.warning(
            "memory.status: load_config raised — returning empty entries",
            exc_info=True,
        )
        return None
    return getattr(cfg, "memory", None)


def _collect_entries() -> list[dict[str, Any]]:
    """Read MEMORY.md / USER.md and project to wire shape.

    Returned entries are sorted by ``target`` for stable client rendering
    (alphabetical: MEMORY.md before USER.md). Mirrors
    :meth:`opencomputer.gateway.wire_server.WireServer._collect_memory_status`
    field-for-field so the wire and REST views agree.
    """
    mem_cfg = _load_memory_config()
    if mem_cfg is None:
        return []

    targets = [
        (
            "MEMORY.md",
            getattr(mem_cfg, "declarative_path", None),
            getattr(mem_cfg, "memory_char_limit", 4000),
        ),
        (
            "USER.md",
            getattr(mem_cfg, "user_path", None),
            getattr(mem_cfg, "user_char_limit", 2000),
        ),
    ]

    entries: list[dict[str, Any]] = []
    for target, path, limit in targets:
        if path is None:
            logger.debug(
                "memory.status: %s path missing from MemoryConfig — skipping",
                target,
            )
            continue
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as exc:
            logger.warning(
                "memory.status: failed to read %s (%s): %s — omitting from result",
                target,
                path,
                exc,
            )
            continue
        status = cap_status(text, limit=limit, file_name=target)
        entries.append(
            {
                "target": status.file_name,
                "content_size": status.bytes_used,
                "cap_limit": status.bytes_limit,
                "pct": status.pct,
                "paragraph_count": status.paragraph_count,
            }
        )
    entries.sort(key=lambda e: e["target"])
    return entries


@router.get("/memory/status")
async def memory_status() -> dict[str, Any]:
    """Snapshot of MEMORY.md / USER.md cap status for the active profile.

    Always returns 200 with an ``entries`` array (possibly empty). Per-file
    or config-load failures degrade to "missing entry", they do NOT raise.
    See module docstring for the failure-isolation contract.
    """
    return {"entries": _collect_entries()}


__all__ = ["router", "_collect_entries", "_load_memory_config"]
