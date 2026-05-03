"""Playwright trace recording wrapper.

Trace lock is **context-scoped** — start one tab while another is untraced
(in the same context) → second start throws.

Trace zips are written via ``context.tracing.stop(path=...)``. Atomic
durability is Playwright's responsibility; we just enforce single-active.
"""

from __future__ import annotations

import os
from typing import Any

# In-memory per-context flag. Keyed by object identity (id()) since
# Playwright contexts have no convenient stable id.
_active_traces: dict[int, bool] = {}


class TraceAlreadyRunningError(RuntimeError):
    """A second start was attempted while a trace was active on this context."""


class TraceNotRunningError(RuntimeError):
    """Stop was called but no trace was active."""


def _is_active(context: Any) -> bool:
    return _active_traces.get(id(context), False)


async def start_trace(
    context: Any,
    *,
    screenshots: bool = True,
    snapshots: bool = True,
    sources: bool = False,
) -> None:
    if _is_active(context):
        raise TraceAlreadyRunningError(
            "Trace already running. Stop the current trace before starting a new one."
        )
    await context.tracing.start(
        screenshots=screenshots, snapshots=snapshots, sources=sources
    )
    _active_traces[id(context)] = True


async def stop_trace(context: Any, *, path: str) -> str:
    if not _is_active(context):
        raise TraceNotRunningError("No trace is currently running on this context")
    if not path:
        raise ValueError("path is required for stop_trace")
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        await context.tracing.stop(path=abs_path)
    finally:
        _active_traces.pop(id(context), None)
    return abs_path


def is_trace_active(context: Any) -> bool:
    return _is_active(context)


def _reset_for_tests() -> None:
    _active_traces.clear()
