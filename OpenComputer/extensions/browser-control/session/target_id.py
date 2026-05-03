"""``page_target_id`` — get the Chrome target ID for a Playwright Page.

Two paths:

1. CDP ground truth — `context.new_cdp_session(page)` then
   ``Target.getTargetInfo``. Detach is best-effort.

2. HTTP fallback — hit ``<cdp_http_base>/json/list`` and match either
   by URL (when there is exactly one CDP target with that URL) or by
   ordinal position among same-URL targets.

The HTTP fallback handles two real cases:

- Manifest V3 extension contexts where ``Target.attachToBrowserTarget``
  returns ``Not allowed`` so CDP probes fail outright.
- Pages that died mid-CDP-probe; the persistent /json/list survives.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from .helpers import (
    CDP_HTTP_REQUEST_TIMEOUT_MS,
    no_proxy_lease,
    normalize_cdp_http_base,
)

_log = logging.getLogger("opencomputer.browser_control.session.target_id")


async def page_target_id(page: Any, *, cdp_url: str | None = None) -> str | None:
    """Return the Chrome target ID for ``page`` or None.

    Tries CDP first; on failure (extension contexts, dead page,
    Playwright internal error), falls back to ``/json/list`` HTTP if
    ``cdp_url`` is known.
    """
    target_id = await _try_cdp_target_info(page)
    if target_id:
        return target_id

    if not cdp_url:
        return None

    try:
        page_url = page.url  # property
    except Exception:  # noqa: BLE001
        return None

    return await _find_via_target_list(page_url=page_url, cdp_url=cdp_url)


async def _try_cdp_target_info(page: Any) -> str | None:
    context = getattr(page, "context", None)
    if context is None:
        return None
    new_cdp_session = getattr(context, "new_cdp_session", None)
    if new_cdp_session is None:
        return None
    try:
        session = await new_cdp_session(page)
    except Exception as exc:  # noqa: BLE001
        _log.debug("page_target_id: new_cdp_session failed: %s", exc)
        return None
    try:
        info = await session.send("Target.getTargetInfo")
    except Exception as exc:  # noqa: BLE001
        _log.debug("page_target_id: Target.getTargetInfo failed: %s", exc)
        info = None
    finally:
        try:
            await session.detach()
        except Exception:  # noqa: BLE001
            pass
    if not isinstance(info, dict):
        return None
    target_info = info.get("targetInfo")
    if not isinstance(target_info, dict):
        return None
    tid = target_info.get("targetId")
    return tid if isinstance(tid, str) and tid else None


async def _find_via_target_list(*, page_url: str, cdp_url: str) -> str | None:
    base = normalize_cdp_http_base(cdp_url).rstrip("/")
    if not base:
        return None
    try:
        parts = urlsplit(base)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None

    timeout_s = CDP_HTTP_REQUEST_TIMEOUT_MS / 1000.0
    async with no_proxy_lease(base):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(f"{base}/json/list")
        except (httpx.HTTPError, OSError) as exc:
            _log.debug("page_target_id: /json/list fetch failed: %s", exc)
            return None
    if resp.status_code != 200:
        return None
    try:
        targets = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(targets, list):
        return None

    matches = [
        t
        for t in targets
        if isinstance(t, dict) and isinstance(t.get("id"), str) and t.get("url") == page_url
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]["id"]
    # Ambiguous — return the first deterministically. A caller that
    # cares about ordinality will have used CDP path and won't reach here.
    return matches[0]["id"]
