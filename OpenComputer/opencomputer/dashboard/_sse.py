"""SSE (Server-Sent Events) helpers for the dashboard (Wave 6.D-β).

FastAPI doesn't ship its own SSE primitive but ``StreamingResponse``
with ``media_type="text/event-stream"`` does the job. We avoid adding
``sse-starlette`` as a dep — vanilla event encoding is ~30 lines of
helper.

Two patterns supported:

1. **mtime-watch**: poll a file's mtime at an interval, emit an event
   each time it changes. Used for profile.yaml + sessions.db where
   we don't have a native change-notification API.

2. **manual-emit**: caller drives the stream by calling ``put()`` on
   a queue. Useful for tests + future event-driven sources.

Format on the wire (per the EventSource spec)::

    event: <name>
    data: <json>
    \n

Browsers reconnect automatically on disconnect; we don't need to
implement reconnect logic on the server side.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.dashboard.sse")


def encode_sse(event: str, data: Any) -> bytes:
    """Encode one SSE message. ``data`` is JSON-serialized."""
    body = json.dumps(data, default=str)
    return f"event: {event}\ndata: {body}\n\n".encode()


def encode_keepalive() -> bytes:
    """SSE comment line — keeps proxies from closing the stream as idle."""
    return b": keepalive\n\n"


async def mtime_watch(
    path: Path,
    *,
    poll_interval: float = 1.0,
    keepalive_interval: float = 15.0,
    initial_emit: bool = True,
    payload_fn: Callable[[Path], Awaitable[Any]] | None = None,
) -> AsyncIterator[bytes]:
    """Yield SSE-encoded bytes whenever ``path``'s mtime changes.

    Args:
        path: file to watch. Missing file = mtime 0.0; if it appears
            later we treat that as a change.
        poll_interval: seconds between mtime checks. 1.0 is plenty for
            human-driven config edits.
        keepalive_interval: emit a keepalive comment line at least this
            often so HTTP proxies don't close the idle connection.
        initial_emit: True (default) → emit one event on first connect
            so the client gets fresh state without waiting for the
            next mtime change.
        payload_fn: optional async callable that builds the event
            payload from the path. Defaults to a small ``{"mtime": ts,
            "exists": bool}`` blob. Custom payload_fn lets callers
            send the actual updated state inline.
    """
    last_mtime: float | None = None
    last_keepalive = 0.0

    async def _build_payload() -> Any:
        if payload_fn is not None:
            try:
                return await payload_fn(path)
            except Exception:  # noqa: BLE001
                logger.exception("sse mtime_watch payload_fn raised")
                return {"path": str(path), "error": "payload-build-failed"}
        try:
            stat = path.stat()
            return {"path": str(path), "mtime": stat.st_mtime, "exists": True}
        except FileNotFoundError:
            return {"path": str(path), "exists": False}

    if initial_emit:
        yield encode_sse("change", await _build_payload())
        try:
            last_mtime = path.stat().st_mtime
        except FileNotFoundError:
            last_mtime = None

    elapsed = 0.0
    while True:
        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return
        elapsed += poll_interval

        try:
            current = path.stat().st_mtime
            exists = True
        except FileNotFoundError:
            current = None
            exists = False

        if current != last_mtime or (exists != (last_mtime is not None)):
            try:
                yield encode_sse("change", await _build_payload())
            except Exception:  # noqa: BLE001
                logger.exception("sse mtime_watch emit failed")
            last_mtime = current

        # Periodic keepalive — covers the case where mtime never moves.
        if elapsed - last_keepalive >= keepalive_interval:
            yield encode_keepalive()
            last_keepalive = elapsed


__all__ = ["encode_sse", "encode_keepalive", "mtime_watch"]
