"""Default HTTP fetcher for recipes.

For v1 the fetcher just does GET + parse-JSON. Future work plugs in a
Playwright-page-aware fetcher that runs requests through the user's
logged-in Chrome via CDP.

NOTE: this is a SYNC fetcher. Calling it from inside an async context
(e.g. the agent loop) would block the event loop. v1 callers are CLI
commands which are sync. For async callers, future work adds an
async fetcher built on httpx.AsyncClient or routes through a
Playwright page.
"""

from __future__ import annotations

from typing import Any

import httpx


def httpx_fetcher(url: str) -> Any:
    """GET + parse JSON; raise on non-2xx."""
    resp = httpx.get(url, follow_redirects=True, timeout=15.0)
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    if "application/json" in ct or url.endswith(".json"):
        return resp.json()
    return resp.text
