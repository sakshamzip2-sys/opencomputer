"""Streaming-stall watchdog utility (Wave 3, 2026-05-08).

Wraps an async iterator (typically a provider's streaming response) and
raises :class:`plugin_sdk.StreamStaleError` if no chunk arrives for
``stale_timeout_seconds``.

The watchdog is *opt-in*: providers set
:attr:`plugin_sdk.BaseProvider.stale_timeout_seconds` (default ``None``)
to enable it. When ``None``, ``stream_with_watchdog`` is a pass-through.

Why this is separate from ``request_timeout_seconds``:
- ``request_timeout_seconds`` is the full-request HTTP cap (httpx); fires
  when the connection dies or the entire request takes too long.
- ``stale_timeout_seconds`` fires when the connection is *alive* but the
  stream has stopped emitting tokens — i.e. an LLM-side hang. Common
  failure mode on local model servers under memory pressure.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TypeVar

from plugin_sdk.provider_contract import StreamStaleError

_T = TypeVar("_T")


async def stream_with_watchdog(  # noqa: UP047 — PEP 695 generic syntax not used elsewhere in this codebase yet
    source: AsyncIterator[_T],
    *,
    stale_timeout_seconds: float | None,
    provider_name: str,
) -> AsyncIterator[_T]:
    """Yield from ``source``; raise StreamStaleError on idle.

    When ``stale_timeout_seconds`` is None, this is a pass-through —
    no watchdog overhead. Otherwise, each ``__anext__`` is wrapped in
    ``asyncio.wait_for(..., timeout=stale_timeout_seconds)``; on
    :class:`asyncio.TimeoutError`, raises
    :class:`StreamStaleError`.

    The wrapper preserves ``source``'s exception semantics: any
    exception other than the watchdog's own propagates as-is.
    """
    if stale_timeout_seconds is None:
        async for chunk in source:
            yield chunk
        return

    while True:
        try:
            chunk = await asyncio.wait_for(
                source.__anext__(),
                timeout=stale_timeout_seconds,
            )
        except StopAsyncIteration:
            return
        except TimeoutError as e:
            raise StreamStaleError(
                provider_name=provider_name,
                stale_seconds=stale_timeout_seconds,
            ) from e
        yield chunk


def estimate_progress_age(last_chunk_at: float) -> float:
    """Return seconds elapsed since ``last_chunk_at`` (monotonic clock)."""
    return time.monotonic() - last_chunk_at
