"""GET /api/v1/logs — Server-Sent log feed.

Attaches an in-memory ring-buffer handler to the root logger. SSE clients
receive log records as they're produced. The buffer caps at 5000 records
so a long-running dashboard session doesn't accumulate memory.

The handler is a module-level singleton with a guard against duplicate
attachment in test harnesses that build_app() multiple times in the same
process (which would otherwise duplicate log lines N times).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from opencomputer.dashboard._sse import encode_keepalive, encode_sse

if TYPE_CHECKING:
    pass

router = APIRouter(prefix="/api/v1", tags=["logs"])

_BUF: deque[dict] = deque(maxlen=5000)
_NEXT_SEQ = 0


class _DashboardLogHandler(logging.Handler):
    """Ring-buffer log handler — captures records for SSE stream."""

    def emit(self, record: logging.LogRecord) -> None:
        global _NEXT_SEQ
        try:
            msg = self.format(record)
        except Exception:
            return
        entry = {
            "seq": _NEXT_SEQ,
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        _NEXT_SEQ += 1
        _BUF.append(entry)


_HANDLER = _DashboardLogHandler()
_HANDLER.setFormatter(logging.Formatter("%(message)s"))
_HANDLER.setLevel(logging.DEBUG)


def _ensure_handler_attached() -> None:
    """Attach the dashboard log handler exactly once.

    Tests build_app multiple times; without de-dup the same record is
    handled N times by N copies of this handler.
    """
    root = logging.getLogger()
    if _HANDLER not in root.handlers:
        root.addHandler(_HANDLER)


_ensure_handler_attached()


@router.get("/logs")
async def stream_logs(
    since: int = Query(0, ge=0, description="Yield records with seq > since"),
    level: str | None = Query(
        None, description="Comma-separated levels to include (DEBUG,INFO,WARNING,ERROR)"
    ),
):
    """SSE endpoint for live log records."""
    _ensure_handler_attached()

    levels_set: set[str] | None = None
    if level:
        levels_set = {lv.strip().upper() for lv in level.split(",") if lv.strip()}

    async def gen():
        last_sent = since - 1
        last_keepalive = time.monotonic()
        while True:
            for entry in list(_BUF):
                if entry["seq"] <= last_sent:
                    continue
                if levels_set and entry["level"] not in levels_set:
                    last_sent = entry["seq"]
                    continue
                yield encode_sse("log", entry)
                last_sent = entry["seq"]
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if now - last_keepalive >= 15.0:
                yield encode_keepalive()
                last_keepalive = now

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/logs/recent")
async def recent_logs(
    limit: int = Query(100, ge=1, le=1000),
    level: str | None = Query(None),
) -> dict:
    """Non-SSE: return a slice of recent log records (for initial paint)."""
    _ensure_handler_attached()
    levels_set: set[str] | None = None
    if level:
        levels_set = {lv.strip().upper() for lv in level.split(",") if lv.strip()}
    rows = [e for e in _BUF if not levels_set or e["level"] in levels_set]
    return {"items": rows[-limit:], "limit": limit}
