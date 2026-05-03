"""HTTP response body reader.

NOT an envelope normalizer (the deep pass corrected the first-pass note —
there's no envelope at all in OpenClaw). This file is just the
``response_body_via_playwright`` helper that subscribes to
``page.on("response", ...)``, waits for a URL match, then reads the body.

Used by the future ``POST /response/body`` route in W2b.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .._utils import url_match

_DEFAULT_TIMEOUT_MS = 20_000
_DEFAULT_MAX_BYTES = 200_000
_MIN_MAX_BYTES = 1
_MAX_MAX_BYTES = 5_000_000


def _clamp_max_bytes(value: int | None) -> int:
    if value is None:
        return _DEFAULT_MAX_BYTES
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_BYTES
    return max(_MIN_MAX_BYTES, min(_MAX_MAX_BYTES, n))


async def read_response_body(
    page: Any,
    *,
    url_pattern: str,
    pattern_mode: str = "substring",
    timeout_ms: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Wait for the next response whose URL matches; read body up to ``max_bytes``.

    Returns ``{url, status, headers, body, truncated}``. ``body`` is decoded
    UTF-8 (errors='replace') so the agent always gets a string. Use the
    raw bytes via the underlying API directly if you need binary.
    """
    timeout_s = max(0.5, (timeout_ms or _DEFAULT_TIMEOUT_MS) / 1000.0)
    cap = _clamp_max_bytes(max_bytes)

    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    def listener(response: Any) -> None:
        try:
            url = getattr(response, "url", "")
            if not isinstance(url, str):
                return
            if not url_match(url_pattern, url, mode=pattern_mode):  # type: ignore[arg-type]
                return
            if not fut.done():
                fut.set_result(response)
        except Exception:
            pass

    on = getattr(page, "on", None)
    off = getattr(page, "remove_listener", None) or getattr(page, "off", None)
    if not callable(on):
        raise RuntimeError("page does not expose .on() — cannot subscribe")
    on("response", listener)
    try:
        try:
            response = await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"timed out waiting for response matching {url_pattern!r}"
            ) from exc
    finally:
        if callable(off):
            try:
                off("response", listener)
            except Exception:
                pass

    body_bytes = await response.body()
    truncated = len(body_bytes) > cap
    if truncated:
        body_bytes = body_bytes[:cap]
    headers = await response.all_headers() if hasattr(response, "all_headers") else {}
    return {
        "url": getattr(response, "url", ""),
        "status": getattr(response, "status", None),
        "headers": dict(headers) if headers else {},
        "body": body_bytes.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }
