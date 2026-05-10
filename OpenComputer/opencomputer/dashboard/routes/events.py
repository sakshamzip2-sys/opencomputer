"""GET /api/v1/events — multiplex TypedEventBus to a single SSE stream.

Subscribes to the process-wide :func:`opencomputer.ingestion.bus.get_default_bus`
on connect, fans out events as SSE messages, unsubscribes on disconnect.
Supports topic filtering via ``?topics=glob1,glob2`` (fnmatch syntax).

**Wire shape (Tier-A of 2026-05-10 memory-observability follow-through):**

The projection is a flat dict of every :class:`plugin_sdk.ingestion.SignalEvent`
field, including subclass-specific fields. Existing consumers that read base
fields (``event_type``, ``event_id``, ``timestamp``, ``session_id``, ``source``,
``metadata``) see them at unchanged paths — purely additive. Subclass-specific
fields (e.g. ``compaction_delta`` on ``MemoryWriteEvent``, ``tool_name`` on
``ToolCallEvent``) are now visible at the top level instead of being silently
stripped.

**Privacy posture:** every ``SignalEvent`` subclass is designed for in-process
bus exposure (see per-class docstrings — ``MemoryWriteEvent`` carries
``content_size`` not content; ``MessageSignalEvent`` carries ``content_length``
not content; ``ForegroundAppEvent`` carries ``window_title_hash`` not the raw
title). The SSE endpoint adds no new exposure surface beyond what existing
in-process wildcard subscribers already see. The endpoint itself binds 127.0.0.1
by default per ``cli_dashboard.py``.

**Failure isolation:** event projection catches every exception and falls back to
the legacy 6-field projection so a misbehaving subclass never breaks the stream.
The encoder uses ``json.dumps(..., default=str)`` so non-JSON-native types (Path,
datetime, custom classes) coerce to strings instead of crashing the response.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from opencomputer.dashboard._sse import encode_keepalive, encode_sse

router = APIRouter(prefix="/api/v1", tags=["events"])
logger = logging.getLogger("opencomputer.dashboard.routes.events")


def _get_bus():
    """Dashboard-internal hook so tests can monkeypatch a fresh bus."""
    from opencomputer.ingestion.bus import get_default_bus

    return get_default_bus()


def _legacy_projection(ev: Any) -> dict[str, Any]:
    """Pre-Tier-A 6-field projection — used as the fallback path.

    Surfaces only the base :class:`SignalEvent` fields. Kept on the failure
    path so a non-dataclass or non-serializable event still produces *some*
    SSE traffic (event_type alone is enough for a consumer to know the
    event happened).
    """
    return {
        "event_type": getattr(ev, "event_type", ""),
        "event_id": getattr(ev, "event_id", ""),
        "timestamp": getattr(ev, "timestamp", time.time()),
        "session_id": getattr(ev, "session_id", None),
        "source": getattr(ev, "source", ""),
        "metadata": dict(getattr(ev, "metadata", {})),
    }


def project_event(ev: Any) -> dict[str, Any]:
    """Project a :class:`SignalEvent` into a JSON-serializable dict for SSE.

    Surfaces every dataclass field (base + subclass-specific). On failure
    — non-dataclass event, missing required attributes, or any other
    exception during ``dataclasses.asdict`` — falls back to the legacy
    6-field projection and logs at WARNING so a regression in the event
    layer is noticed but doesn't break the consumer.

    This is the SSE wire contract. Refactored out of the route closure so
    it's unit-testable in isolation.
    """
    if not dataclasses.is_dataclass(ev):
        logger.warning(
            "events SSE: non-dataclass on bus event_type=%s — using legacy projection",
            getattr(ev, "event_type", type(ev).__name__),
        )
        return _legacy_projection(ev)
    try:
        item = dataclasses.asdict(ev)
    except Exception:  # noqa: BLE001 — asdict on a malformed event must not break the stream
        logger.warning(
            "events SSE: asdict failed for event_type=%s — using legacy projection",
            getattr(ev, "event_type", "?"),
            exc_info=True,
        )
        return _legacy_projection(ev)
    # asdict gives us a dict[str, Any]. Defensive: ensure base fields are
    # present even if a buggy subclass somehow shadowed them with non-default
    # field declarations. asdict respects the dataclass contract so this is
    # belt-and-braces, not load-bearing.
    for k, default in (
        ("event_type", ""),
        ("event_id", ""),
        ("timestamp", time.time()),
        ("session_id", None),
        ("source", ""),
        ("metadata", {}),
    ):
        item.setdefault(k, default)
    return item


@router.get("/events")
async def events(
    topics: str | None = Query(
        None, description="Comma-separated fnmatch globs to filter by event_type"
    ),
):
    """SSE multiplex from TypedEventBus.

    Each subscribed event goes through :func:`project_event` — a flat dict
    of every dataclass field on the event. See module docstring for the
    wire-shape contract and privacy posture.
    """
    bus = _get_bus()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
    patterns = [p.strip() for p in topics.split(",")] if topics else ["*"]

    def _on_event(ev) -> None:
        try:
            item = project_event(ev)
        except Exception:  # noqa: BLE001 — defense-in-depth; project_event is already isolated
            logger.exception("events SSE: project_event raised unexpectedly")
            return
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass  # drop on backpressure rather than block the bus

    # subscribe_pattern returns a Subscription with .unsubscribe()
    subs = [bus.subscribe_pattern(p, _on_event) for p in patterns]

    async def gen():
        last_keepalive = time.monotonic()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield encode_sse("event", item)
                except TimeoutError:
                    pass
                now = time.monotonic()
                if now - last_keepalive >= 15.0:
                    yield encode_keepalive()
                    last_keepalive = now
        finally:
            for sub in subs:
                try:
                    sub.unsubscribe()
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(gen(), media_type="text/event-stream")


__all__ = ["router", "project_event"]
