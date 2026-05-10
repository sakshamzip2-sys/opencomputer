"""GET /api/v1/events — multiplex TypedEventBus to a single SSE stream.

Subscribes to the process-wide :func:`opencomputer.ingestion.bus.get_default_bus`
on connect, fans out events as SSE messages, unsubscribes on disconnect.
Supports topic filtering via ``?topics=glob1,glob2`` (fnmatch syntax).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from opencomputer.dashboard._sse import encode_keepalive, encode_sse

router = APIRouter(prefix="/api/v1", tags=["events"])


def _get_bus():
    """Dashboard-internal hook so tests can monkeypatch a fresh bus."""
    from opencomputer.ingestion.bus import get_default_bus

    return get_default_bus()


@router.get("/events")
async def events(
    topics: str | None = Query(
        None, description="Comma-separated fnmatch globs to filter by event_type"
    ),
):
    """SSE multiplex from TypedEventBus."""
    bus = _get_bus()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
    patterns = [p.strip() for p in topics.split(",")] if topics else ["*"]

    def _on_event(ev) -> None:
        try:
            queue.put_nowait(
                {
                    "event_type": getattr(ev, "event_type", ""),
                    "event_id": getattr(ev, "event_id", ""),
                    "timestamp": getattr(ev, "timestamp", time.time()),
                    "session_id": getattr(ev, "session_id", None),
                    "source": getattr(ev, "source", ""),
                    "metadata": dict(getattr(ev, "metadata", {})),
                }
            )
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
